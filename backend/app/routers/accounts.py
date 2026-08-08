import hashlib
import datetime as dt
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from .. import security, broker_manager
from ..database import get_db, Account, Order
from ..brokers import BrokerError
from ..schemas import AccountCreate, AccountUpdate, AccountOut
from ..auth import require_login
from ..strategy import engine as strategy_engine

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(require_login)])

ALICE_BLUE_VENDOR_URL = "https://ant.aliceblueonline.com/open-api/od/v1/vendor/getUserDetails"


def _validate_sizing(sizing_mode: str | None, capital_pct: float | None, fixed_amount: float | None,
                      max_trades: int | None):
    if sizing_mode is not None and sizing_mode not in ("pct", "fixed_amount"):
        raise HTTPException(400, "sizing_mode must be 'pct' or 'fixed_amount'")
    if capital_pct is not None and not (0 < capital_pct <= 100):
        raise HTTPException(400, "capital_per_trade_pct must be between 0 and 100")
    if fixed_amount is not None and fixed_amount < 0:
        raise HTTPException(400, "fixed_amount_per_trade must be 0 or more")
    if max_trades is not None and max_trades < 1:
        raise HTTPException(400, "max_trades_per_day must be at least 1")


def _estimate_capital_per_trade(account: Account) -> float | None:
    if account.sizing_mode == "fixed_amount":
        return round(account.fixed_amount_per_trade, 2)
    if account.available_funds is None:
        return None
    return round(account.available_funds * (account.capital_per_trade_pct / 100), 2)


def _trades_today(db: Session, account_id: int) -> int:
    start = dt.datetime.combine(dt.date.today(), dt.time.min)
    return db.query(Order).filter(Order.account_id == account_id, Order.ts >= start).count()


def _account_out(db: Session, account: Account) -> AccountOut:
    return AccountOut(
        id=account.id, label=account.label, broker=account.broker, mode=account.mode,
        enabled=account.enabled, status=account.status, status_message=account.status_message,
        available_funds=account.available_funds, sizing_mode=account.sizing_mode,
        capital_per_trade_pct=account.capital_per_trade_pct,
        fixed_amount_per_trade=account.fixed_amount_per_trade,
        max_trades_per_day=account.max_trades_per_day,
        estimated_capital_per_trade=_estimate_capital_per_trade(account),
        trades_today=_trades_today(db, account.id),
    )


@router.get("", response_model=list[AccountOut])
def list_accounts(db: Session = Depends(get_db)):
    return [_account_out(db, a) for a in db.query(Account).all()]


@router.post("", response_model=AccountOut)
def add_account(payload: AccountCreate, db: Session = Depends(get_db)):
    if payload.broker not in ("angel_one", "alice_blue"):
        raise HTTPException(400, "broker must be 'angel_one' or 'alice_blue'")
    if payload.mode not in ("sandbox", "live"):
        raise HTTPException(400, "mode must be 'sandbox' or 'live'")
    _validate_sizing(payload.sizing_mode, payload.capital_per_trade_pct,
                      payload.fixed_amount_per_trade, payload.max_trades_per_day)

    account = Account(
        label=payload.label,
        broker=payload.broker,
        mode=payload.mode,
        cred_client_id=security.encrypt(payload.client_id),
        cred_api_key=security.encrypt(payload.api_key),
        cred_secret=security.encrypt(payload.secret),
        cred_password=security.encrypt(payload.password),
        cred_totp_secret=security.encrypt(payload.totp_secret),
        sizing_mode=payload.sizing_mode,
        capital_per_trade_pct=payload.capital_per_trade_pct,
        fixed_amount_per_trade=payload.fixed_amount_per_trade,
        max_trades_per_day=payload.max_trades_per_day,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return _account_out(db, account)


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(account_id: int, payload: AccountUpdate, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "account not found")
    if payload.label is not None:
        account.label = payload.label
    if payload.mode is not None:
        if payload.mode not in ("sandbox", "live"):
            raise HTTPException(400, "mode must be 'sandbox' or 'live'")
        if payload.mode != account.mode:
            broker_manager.disconnect_account(account.id)
            account.status = "disconnected"
        account.mode = payload.mode
    if payload.enabled is not None:
        account.enabled = payload.enabled
        if not payload.enabled:
            broker_manager.disconnect_account(account.id)
            account.status = "disconnected"

    _validate_sizing(payload.sizing_mode, payload.capital_per_trade_pct,
                      payload.fixed_amount_per_trade, payload.max_trades_per_day)
    if payload.sizing_mode is not None:
        account.sizing_mode = payload.sizing_mode
    if payload.capital_per_trade_pct is not None:
        account.capital_per_trade_pct = payload.capital_per_trade_pct
    if payload.fixed_amount_per_trade is not None:
        account.fixed_amount_per_trade = payload.fixed_amount_per_trade
    if payload.max_trades_per_day is not None:
        account.max_trades_per_day = payload.max_trades_per_day

    db.commit()
    db.refresh(account)
    return _account_out(db, account)


@router.delete("/{account_id}")
def remove_account(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "account not found")
    broker_manager.disconnect_account(account_id)
    db.delete(account)
    db.commit()
    return {"ok": True}


@router.post("/{account_id}/connect", response_model=AccountOut)
def connect_account(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "account not found")
    try:
        conn = broker_manager.connect_account(account)
        account.status = "connected"
        account.status_message = ""
        try:
            account.available_funds = conn.get_available_funds()
        except Exception as e:
            account.status_message = f"connected, but funds fetch failed: {e}"
        # A fresh connection starts a brand new WebSocket feed with an empty
        # subscription set. Without this, prices for whatever's already in
        # today's watchlist (restored from persisted state, or scanned
        # earlier) stay frozen until the next full scan re-subscribes them.
        strategy_engine.resubscribe_live_feed(conn)
    except BrokerError as e:
        account.status = "error"
        account.status_message = str(e)
    except Exception as e:
        account.status = "error"
        account.status_message = f"unexpected error: {e}"
    db.commit()
    db.refresh(account)
    return _account_out(db, account)


@router.post("/{account_id}/funds", response_model=AccountOut)
def refresh_funds(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "account not found")
    conn = broker_manager.get_connection(account_id)
    if not conn:
        raise HTTPException(400, "account is not connected")
    try:
        account.available_funds = conn.get_available_funds()
    except BrokerError as e:
        account.status_message = f"funds fetch failed: {e}"
    db.commit()
    db.refresh(account)
    return _account_out(db, account)


def _alice_blue_login_url(account: Account) -> str:
    app_code = security.decrypt(account.cred_api_key)
    if not app_code:
        raise HTTPException(400, "App Code (API Key) is not set on this account")
    return f"https://ant.aliceblueonline.com/?appcode={app_code}"


@router.get("/{account_id}/alice_blue/login-url")
def alice_blue_login_url(account_id: int, request: Request, db: Session = Depends(get_db)):
    """Returns the Alice Blue login redirect URL as JSON instead of
    redirecting straight away, so the dashboard can show it to the user
    (copy it, open it on another device/browser, etc.) rather than just
    silently navigating away. Also returns the Redirect URL that must be
    registered in Alice Blue's own developer console for this app/appcode -
    a one-time setup step outside this dashboard, and the most common
    reason the login redirect fails (wrong or missing Redirect URL there)."""
    account = db.get(Account, account_id)
    if not account or account.broker != "alice_blue":
        raise HTTPException(404, "alice_blue account not found")
    base = str(request.base_url).rstrip("/")
    return {
        "url": _alice_blue_login_url(account),
        "redirect_url": f"{base}/api/accounts/alice_blue/callback",
    }


@router.get("/{account_id}/alice_blue/login")
def alice_blue_login(account_id: int, db: Session = Depends(get_db)):
    """Step 1 of Alice Blue's OAuth-style flow: send the browser to Alice
    Blue's login page. It redirects back to /alice_blue/callback with
    authCode + userId once the user logs in there."""
    account = db.get(Account, account_id)
    if not account or account.broker != "alice_blue":
        raise HTTPException(404, "alice_blue account not found")
    return RedirectResponse(_alice_blue_login_url(account))


def _complete_alice_blue_login(account: Account, authCode: str, userId: str, db: Session):
    """Exchange authCode/userId plus our app secret for a session token
    (SHA-256 checksum of userId+authCode+apiSecret, per Alice Blue's vendor
    API docs), then connect the account.

    Every failure here - the exchange call itself, Alice Blue rejecting the
    login, or the broker connect step - records the real reason on the
    account and redirects back to the dashboard instead of showing a raw
    error page, since a redirect straight to an HTTPException response was
    reading as "the login just broke" with no way to see why or retry.
    """
    try:
        api_secret = security.decrypt(account.cred_secret)
        checksum = hashlib.sha256(f"{userId}{authCode}{api_secret}".encode()).hexdigest()
        resp = httpx.post(ALICE_BLUE_VENDOR_URL, json={"checkSum": checksum}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("stat") != "Ok" or not data.get("userSession"):
            raise RuntimeError(data.get("emsg") or f"Alice Blue login failed: {data}")

        account.cred_client_id = security.encrypt(data.get("clientId") or userId)
        account.cred_session_token = security.encrypt(data["userSession"])
        conn = broker_manager.connect_account(account)
        account.status = "connected"
        account.status_message = ""
        account.available_funds = conn.get_available_funds()
    except Exception as e:
        account.status = "error"
        account.status_message = f"Alice Blue login failed: {e}"
    db.commit()
    return RedirectResponse("/")


@router.get("/alice_blue/callback")
def alice_blue_callback_static(authCode: str = "", userId: str = "", appcode: str = "",
                                db: Session = Depends(get_db)):
    """Account-agnostic callback for Alice Blue's redirect. Alice Blue's
    Redirect URL is a fixed value configured once in their own developer
    console per app (appcode) - it can't contain our internal account id,
    so this is the URL to register there: it resolves the right account by
    matching the appcode Alice Blue sends back against each stored Alice
    Blue account's own API key instead of needing the id in the path."""
    if not authCode or not userId or not appcode:
        raise HTTPException(400, "missing authCode/userId/appcode from Alice Blue redirect")
    candidates = db.query(Account).filter(Account.broker == "alice_blue").all()
    account = next((a for a in candidates if security.decrypt(a.cred_api_key) == appcode), None)
    if not account:
        raise HTTPException(404, f"No Alice Blue account found matching appcode {appcode}")
    return _complete_alice_blue_login(account, authCode, userId, db)


@router.get("/{account_id}/alice_blue/callback")
def alice_blue_callback(account_id: int, authCode: str = "", userId: str = "",
                         db: Session = Depends(get_db)):
    """Step 2 (legacy, per-account path): Alice Blue redirects here with
    authCode + userId if the Redirect URL was registered with a specific
    account id in it. Prefer registering the static /alice_blue/callback
    route above instead - it works regardless of which account initiated
    the login."""
    account = db.get(Account, account_id)
    if not account or account.broker != "alice_blue":
        raise HTTPException(404, "alice_blue account not found")
    if not authCode or not userId:
        raise HTTPException(400, "missing authCode/userId from Alice Blue redirect")
    return _complete_alice_blue_login(account, authCode, userId, db)


@router.post("/{account_id}/disconnect", response_model=AccountOut)
def disconnect_account(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "account not found")
    broker_manager.disconnect_account(account_id)
    account.status = "disconnected"
    db.commit()
    db.refresh(account)
    return _account_out(db, account)
