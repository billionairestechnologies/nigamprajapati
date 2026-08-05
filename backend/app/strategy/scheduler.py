import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from . import engine
from ..database import SessionLocal

log = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None


def _safe(fn):
    def wrapper():
        try:
            fn()
        except Exception:
            log.exception("Scheduled job %s failed", fn.__name__)
    return wrapper


def start():
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return
    # A BackgroundScheduler that's been shutdown() can't be safely restarted -
    # APScheduler's executors/job stores don't reinitialize, so every job
    # submitted after a restart silently fails ("Error submitting job ... to
    # executor 'default'") even though start() itself reports success. Build
    # a fresh instance every time the kill switch is un-killed instead of
    # reusing the old (possibly shut-down) one.
    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    # Read scan_time/squareoff_time fresh from settings each time the engine
    # (re-)starts, so editing them in the Settings tab takes effect next time.
    with SessionLocal() as db:
        settings = engine.get_settings(db)
        scan_time, squareoff_time = settings.scan_time, settings.squareoff_time
        premarket_time, postmarket_time = settings.premarket_scan_time, settings.postmarket_scan_time
    scan_h, scan_m = map(int, scan_time.split(":"))
    sq_h, sq_m = map(int, squareoff_time.split(":"))
    pre_h, pre_m = map(int, premarket_time.split(":"))
    post_h, post_m = map(int, postmarket_time.split(":"))

    # Informational only - refreshes index/gainers/losers/sector display
    # before the market properly opens. Never touches the trading watchlist.
    _scheduler.add_job(_safe(engine.run_premarket_snapshot), CronTrigger(hour=pre_h, minute=pre_m, day_of_week="mon-fri"))

    # Step 1 + 2: index/sector/stock scan, once at 9:30
    _scheduler.add_job(_safe(engine.run_market_scan), CronTrigger(hour=scan_h, minute=scan_m, day_of_week="mon-fri"))

    # Step 3 + 4: 5-min confirmation + execution, every 5 minutes between 9:30 and 3:15
    _scheduler.add_job(
        _safe(engine.run_confirmation_pass),
        CronTrigger(day_of_week="mon-fri", hour=f"{scan_h}-{sq_h}", minute="*/5"),
    )

    # Position monitoring (stop-loss / daily target), every minute during the trading window
    _scheduler.add_job(
        _safe(engine.monitor_positions),
        CronTrigger(day_of_week="mon-fri", hour=f"{scan_h}-{sq_h}", minute="*"),
    )

    # Square-off at the configured time, no new entries after
    _scheduler.add_job(_safe(engine.square_off_all), CronTrigger(hour=sq_h, minute=sq_m, day_of_week="mon-fri"))

    # Retry window: square_off_all only touches orders still status=="open",
    # so re-running it is safe/idempotent. If the first attempt's exit order
    # failed (broker down, rate-limited, rejected - see _close_order), that
    # position would otherwise stay open indefinitely with no further retry.
    # Re-run every 5 minutes for the hour after square-off to catch it.
    # No wraparound past 23 - CronTrigger's hour range can't express "23-0",
    # so just retry within the square-off hour itself in that edge case.
    retry_hour = sq_h + 1 if sq_h < 23 else sq_h
    _scheduler.add_job(
        _safe(engine.square_off_all),
        CronTrigger(day_of_week="mon-fri", hour=f"{sq_h}-{retry_hour}", minute="*/5"),
    )

    # Informational only - captures real closing prices instead of leaving
    # the dashboard frozen at whatever the last tick was a few minutes
    # before close. Never touches the trading watchlist (day's already over).
    _scheduler.add_job(_safe(engine.run_postmarket_snapshot), CronTrigger(hour=post_h, minute=post_m, day_of_week="mon-fri"))

    _scheduler.start()
    log.info("Scheduler started")


def shutdown():
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
