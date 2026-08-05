import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from fastapi import FastAPI, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse

from .database import init_db, SessionLocal, EngineState, Account
from .routers import accounts, engine as engine_router
from . import auth
from . import config
from . import broker_manager
from .strategy import engine as strategy_engine
from .strategy import scheduler
from . import instrument_master

log = logging.getLogger("startup")

LOG_DIR = config.DATA_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
_file_handler = TimedRotatingFileHandler(LOG_DIR / "app.log", when="midnight", backupCount=30)
_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_file_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Intraday Equity Scanner")

init_db()

app.include_router(accounts.router)
app.include_router(engine_router.router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.on_event("startup")
def _attach_file_log_handler():
    # uvicorn replaces root logger handlers on startup (after this module is
    # imported), so the file handler must be attached after that, not at
    # import time - otherwise it gets silently dropped.
    logging.getLogger().addHandler(_file_handler)


@app.on_event("startup")
def _restore_scan_state():
    strategy_engine.load_persisted_state()


@app.on_event("startup")
def _refresh_master_data():
    # Downloads the scrip master fresh once per calendar day (see
    # instrument_master.py) - doing it eagerly at startup means a network
    # failure shows up immediately in the logs instead of surfacing later
    # as a mysterious "symbol not found" deep inside a scan. Wrapped in
    # try/except: a startup event that raises aborts the whole app (and
    # every startup event after it, including broker reconnect and
    # scheduler resume below) - a down internet connection or a slow/flaky
    # scrip-master endpoint at launch must not prevent the dashboard from
    # coming up at all. Any stale/missing master data just surfaces later
    # as "symbol not found" during a scan, which is recoverable.
    try:
        instrument_master.ensure_fresh_for_today()
    except Exception as e:
        log.error("Could not refresh instrument master data on startup: %s", e)


@app.on_event("startup")
def _reconnect_broker_accounts():
    # Broker connections live only in memory - restore them on every restart
    # so a previously-connected account doesn't need a manual reconnect
    # before scanning/trading can resume. Credentials are stored encrypted.
    with SessionLocal() as db:
        accounts = db.query(Account).filter(Account.enabled == True).all()  # noqa: E712
        for account in accounts:
            try:
                conn = broker_manager.connect_account(account)
                account.status = "connected"
                account.status_message = ""
                try:
                    account.available_funds = conn.get_available_funds()
                except Exception:
                    pass
                strategy_engine.resubscribe_live_feed(conn)
                log.info("Reconnected broker account %s on startup", account.label)
            except Exception as e:
                account.status = "error"
                account.status_message = f"auto-reconnect failed: {e}"
                log.error("Could not auto-reconnect %s: %s", account.label, e)
        db.commit()


@app.on_event("startup")
def _resume_scheduler_if_was_active():
    # "active" is a persisted DB flag showing whether the engine was
    # started, but APScheduler itself is purely in-memory - every restart
    # begins with it stopped, regardless of what the flag says. Resuming
    # here means the strategy runs itself daily (9:30 scan, 5-min
    # confirmation, square-off) with no manual click needed on ordinary
    # restarts. Deliberately NOT unconditional: if you click Stop, that
    # stays respected across a restart/crash too - a live-money system
    # shouldn't silently resume trading you explicitly paused.
    with SessionLocal() as db:
        state = db.query(EngineState).first()
        if state and state.active:
            try:
                scheduler.start()
                if not state.paper_mode:
                    # Auto-resuming is deliberate even across a crash/reboot
                    # (see comment above) - but doing that in LIVE mode with
                    # zero visible notice is the one case that actually
                    # matters: the client should know real-money trading
                    # just restarted itself, not discover it later.
                    settings = strategy_engine.get_settings(db)
                    strategy_engine.send_webhook(settings, "engine_auto_resumed_live", {
                        "note": "The trading engine auto-resumed in LIVE mode after a restart/crash. "
                                "It was left running before the restart and paper mode is currently OFF.",
                    })
                    log.warning("Engine auto-resumed in LIVE mode after restart (was left active, paper_mode=False)")
            except Exception as e:
                # Belt-and-suspenders on top of the scan_time < squareoff_time
                # validation in routers/engine.py: a startup event that raises
                # aborts the entire app, so any bad/pre-existing settings
                # combo must not be able to permanently brick every future
                # restart. Worst case here is the engine simply doesn't
                # auto-resume - recoverable from the dashboard - instead of
                # the dashboard never coming up at all.
                log.error("Could not resume scheduler on startup (check Settings for invalid times): %s", e)


@app.get("/")
def dashboard(request: Request):
    # Unlike the API routes (auth.require_login, a plain 401), a page load
    # needs an actual redirect - a browser won't retry a 401 with a login
    # form the way it auto-prompts for HTTP Basic Auth.
    if not auth.session_valid(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse("/login")
    resp = FileResponse(STATIC_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/login")
def login_page(request: Request):
    if auth.session_valid(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse("/")
    resp = FileResponse(STATIC_DIR / "login.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...)):
    if not auth.check_credentials(username, password):
        resp = RedirectResponse("/login?error=1", status_code=303)
        return resp
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE, auth.create_session_token(),
        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
    )
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.middleware("http")
async def no_cache_static(request, call_next):
    # Browsers (Safari especially) cache static assets aggressively by
    # default, which can leave a stale app.js running against a freshly
    # updated index.html. Force revalidation on every request instead.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
def health():
    return {"ok": True}
