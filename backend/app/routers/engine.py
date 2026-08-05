import re
import datetime as dt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db, EngineState, Signal, Order, StrategySettings, DailySummary
from ..schemas import EngineModeUpdate, SignalOut, OrderOut, SettingsOut, SettingsUpdate, PaperModeUpdate, WebhookTestRequest
from ..strategy import engine as strategy_engine
from ..strategy import scanner
from ..strategy import scheduler
from ..auth import require_login
from .. import config, instrument_master

router = APIRouter(prefix="/api/engine", tags=["engine"], dependencies=[Depends(require_login)])

LOG_FILE = config.DATA_DIR / "logs" / "app.log"


@router.get("/state")
def get_state(db: Session = Depends(get_db)):
    # Route through strategy_engine.get_state() (not a raw query) so the
    # day-rollover check runs even when nothing has scanned yet today -
    # otherwise the dashboard could show yesterday's leftover watchlist
    # sitting next to today's (correctly blank) bias.
    state = strategy_engine.get_state(db)
    return {
        "run_mode": state.run_mode,
        "active": state.active,
        "trading_day": state.trading_day,
        "trades_today": state.trades_today,
        "realized_pnl_today": round(state.realized_pnl_today, 2),
        "target_hit": state.target_hit,
        "bias": state.bias,
        "last_scan_ts": state.last_scan_ts,
        "watchlist": strategy_engine.get_watchlist(),
        "paper_mode": state.paper_mode,
    }


@router.get("/scan-progress")
def scan_progress():
    return scanner.get_progress()


@router.post("/mode")
def set_mode(payload: EngineModeUpdate, db: Session = Depends(get_db)):
    if payload.run_mode not in ("manual", "auto"):
        raise HTTPException(400, "run_mode must be 'manual' or 'auto'")
    state = db.query(EngineState).first()
    state.run_mode = payload.run_mode
    db.commit()
    return {"ok": True, "run_mode": state.run_mode}


@router.post("/paper-mode")
def set_paper_mode(payload: PaperModeUpdate, db: Session = Depends(get_db)):
    state = db.query(EngineState).first()
    state.paper_mode = payload.paper_mode
    db.commit()
    return {"ok": True, "paper_mode": state.paper_mode}


@router.get("/history")
def daily_history(db: Session = Depends(get_db)):
    """DailySummary only gets a row for a day once it's rolled over (see
    strategy_engine.get_state), so today never showed up in History until
    tomorrow - the user has to be able to review today's performance
    without waiting for that. Prepend a live-computed row for today."""
    state = strategy_engine.get_state(db)
    today_start = dt.datetime.combine(dt.date.today(), dt.time.min)
    today_signals = db.query(Signal).filter(Signal.ts >= today_start).all()
    today_row = {
        "trading_day": state.trading_day, "bias": state.bias, "trades_taken": state.trades_today,
        "realized_pnl": round(state.realized_pnl_today, 2), "target_hit": state.target_hit,
        "paper_mode": state.paper_mode, "signals_fired": len(today_signals),
        "signals_target_hit": sum(1 for s in today_signals if s.outcome == "target_hit"),
        "signals_sl_hit": sum(1 for s in today_signals if s.outcome == "sl_hit"),
        "is_today": True,
    }
    rows = db.query(DailySummary).order_by(DailySummary.trading_day.desc()).limit(90).all()
    return [today_row] + [
        {
            "trading_day": r.trading_day, "bias": r.bias, "trades_taken": r.trades_taken,
            "realized_pnl": round(r.realized_pnl, 2), "target_hit": r.target_hit,
            "paper_mode": r.paper_mode, "signals_fired": r.signals_fired,
            "signals_target_hit": r.signals_target_hit, "signals_sl_hit": r.signals_sl_hit,
            "is_today": False,
        }
        for r in rows
    ]


@router.post("/start")
def start_engine(db: Session = Depends(get_db)):
    scheduler.start()
    state = db.query(EngineState).first()
    state.active = True
    db.commit()
    return {"ok": True}


@router.post("/stop")
def stop_engine(db: Session = Depends(get_db)):
    scheduler.shutdown()
    state = db.query(EngineState).first()
    state.active = False
    db.commit()
    return {"ok": True}


@router.post("/scan-now")
def scan_now():
    result = strategy_engine.run_market_scan()
    return result or {"ok": False, "reason": "no connected broker"}


@router.post("/confirm-now")
def confirm_now():
    fired = strategy_engine.run_confirmation_pass()
    return {"fired_signal_ids": fired}


@router.get("/scan-detail")
def scan_detail():
    return strategy_engine.get_live_scan_detail()


@router.get("/indicators")
def indicator_status():
    return strategy_engine.get_indicator_status()


@router.get("/candles/{symbol}")
def symbol_candles(symbol: str):
    try:
        return strategy_engine.get_symbol_candles(symbol)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/signals", response_model=list[SignalOut])
def list_signals(day: str | None = None, db: Session = Depends(get_db)):
    """Defaults to TODAY only - the Dashboard's live Signals panel showing
    a week-old signal next to today's live ones (mixed in via a flat "last
    100 ever" query) reads as if it just fired. Pass `day` (YYYY-MM-DD) to
    look at a specific past day instead - used by the History tab's
    expandable rows so a past day's actual signals (symbol, prices,
    outcome) are visible, not just the aggregate counts in DailySummary."""
    if day:
        try:
            start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time.min)
        except ValueError:
            raise HTTPException(400, "day must be YYYY-MM-DD")
    else:
        start = dt.datetime.combine(dt.date.today(), dt.time.min)
    query = db.query(Signal).filter(Signal.ts >= start, Signal.ts < start + dt.timedelta(days=1))
    return query.order_by(Signal.id.desc()).all()


@router.post("/signals/{signal_id}/approve")
def approve_signal(signal_id: int, db: Session = Depends(get_db)):
    signal = db.get(Signal, signal_id)
    if not signal:
        raise HTTPException(404, "signal not found")
    signal.status = "approved"
    db.commit()
    strategy_engine.execute_signal(signal_id)
    return {"ok": True}


@router.post("/signals/{signal_id}/reject")
def reject_signal(signal_id: int):
    strategy_engine.reject_signal(signal_id)
    return {"ok": True}


@router.get("/orders", response_model=list[OrderOut])
def list_orders(day: str | None = None, db: Session = Depends(get_db)):
    """`day` (YYYY-MM-DD) scopes to one trading day's real trades - used by
    the History tab's expandable rows. Without it, the last 200 orders
    across all days (what the Orders tab shows)."""
    query = db.query(Order)
    if day:
        try:
            start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time.min)
        except ValueError:
            raise HTTPException(400, "day must be YYYY-MM-DD")
        query = query.filter(Order.ts >= start, Order.ts < start + dt.timedelta(days=1))
        return query.order_by(Order.id.desc()).all()
    return query.order_by(Order.id.desc()).limit(200).all()


@router.post("/square-off")
def square_off_now():
    strategy_engine.square_off_all()
    return {"ok": True}


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@router.get("/settings", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return db.query(StrategySettings).first()


@router.patch("/settings", response_model=SettingsOut)
def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db)):
    settings = db.query(StrategySettings).first()
    data = payload.model_dump(exclude_unset=True)

    for pct_field in ("daily_profit_target_pct",
                      "sector_move_threshold_pct", "stock_move_threshold_pct", "target_pct", "sl_pct"):
        if pct_field in data and not (0 < data[pct_field] <= 100):
            raise HTTPException(400, f"{pct_field} must be between 0 and 100")
    if "sl_points" in data and data["sl_points"] <= 0:
        raise HTTPException(400, "sl_points must be greater than 0")
    if "target_mode" in data and data["target_mode"] not in ("r_multiple", "fixed_pct"):
        raise HTTPException(400, "target_mode must be 'r_multiple' or 'fixed_pct'")
    if "sl_mode" in data and data["sl_mode"] not in ("candle", "fixed_pct", "fixed_points"):
        raise HTTPException(400, "sl_mode must be 'candle', 'fixed_pct', or 'fixed_points'")
    directional_fields = ("use_ema", "use_vwap", "use_supertrend", "use_rsi")
    resulting = {f: data.get(f, getattr(settings, f)) for f in directional_fields}
    if not any(resulting.values()):
        raise HTTPException(400, "at least one of use_ema/use_vwap/use_supertrend/use_rsi must stay enabled")
    for time_field in ("scan_time", "squareoff_time", "premarket_scan_time", "postmarket_scan_time"):
        if time_field in data and not _TIME_RE.match(data[time_field]):
            raise HTTPException(400, f"{time_field} must be in HH:MM 24-hour format")
    # scan_time must precede squareoff_time - the scheduler builds an
    # hour-range cron trigger from them (see strategy/scheduler.py), which
    # raises at scheduler.start() time if the range is inverted. That call
    # happens unguarded on every app startup (see main.py's
    # _resume_scheduler_if_was_active), so a bad combination saved here
    # would otherwise permanently prevent the app from starting again.
    resulting_scan = data.get("scan_time", settings.scan_time)
    resulting_squareoff = data.get("squareoff_time", settings.squareoff_time)
    if resulting_scan >= resulting_squareoff:
        raise HTTPException(400, "scan_time must be earlier than squareoff_time")
    if "webhook_url" in data and data["webhook_url"] and not data["webhook_url"].startswith(("http://", "https://")):
        raise HTTPException(400, "webhook_url must start with http:// or https://")

    for key, value in data.items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


@router.post("/webhook/test")
def test_webhook(payload: WebhookTestRequest, db: Session = Depends(get_db)):
    settings = db.query(StrategySettings).first()
    url = payload.url or settings.webhook_url
    if not url:
        raise HTTPException(400, "no webhook URL configured or provided")
    # Fire directly at the given URL regardless of the saved enabled/url
    # fields, so "Send Test Webhook" works even before the user has saved
    # settings - build a throwaway settings-shaped object for send_webhook.
    class _Probe:
        webhook_enabled = True
        webhook_url = url
    ok, message = strategy_engine.send_webhook(_Probe(), "test", {"note": "Test webhook from Strategy Terminal"})
    if not ok:
        raise HTTPException(502, message)
    return {"ok": True, "message": message}


@router.post("/master-data/refresh")
def refresh_master_data():
    """Manual redo for the instrument master (symbol -> token) cache -
    normally it refreshes once per calendar day on its own."""
    try:
        instrument_master.refresh()
    except Exception as e:
        raise HTTPException(502, f"Master data refresh failed: {e}")
    return {"ok": True}


@router.get("/logs")
def get_logs(level: str = "all", limit: int = 300):
    """Tails the persistent log file (see main.py's TimedRotatingFileHandler)
    for the dashboard's Strategy/Error Logs panel. `level` filters to
    "error" (ERROR only), "warning" (WARNING+ERROR), or "all"."""
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open(errors="replace") as f:
        lines = f.readlines()[-5000:]  # cap how much we ever scan, file rotates daily anyway

    if level == "error":
        lines = [ln for ln in lines if " ERROR " in ln]
    elif level == "warning":
        lines = [ln for ln in lines if " ERROR " in ln or " WARNING " in ln]

    return [ln.rstrip("\n") for ln in lines[-limit:]]
