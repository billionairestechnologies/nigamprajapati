import datetime as dt
import json
import logging
import threading
import time
import requests
from .. import broker_manager, instrument_master
from ..database import SessionLocal, Account, Signal, Order, EngineState, StrategySettings, DailySummary
from . import scanner
from ..indicators import evaluate_confirmation_stack


def _token_for(symbol: str) -> str:
    row = instrument_master.resolve("NSE", symbol)
    return row["token"] if row else ""

log = logging.getLogger("engine")

# Transient per-day state - fine to keep in-process for a single-machine app.
_watchlist: list[dict] = []
_scan_detail: dict = {"sector_moves": {}, "gainers": [], "losers": []}
_indicator_status: dict[str, dict] = {}  # symbol -> latest evaluate_confirmation_stack() reads
_all_stocks: list[dict] = []            # every scanned stock from the last full scan (symbol/token/sector/day_open)
_index_reads: dict = {}                 # last full scan's per-index reads (name -> {token, day_open, ...})

# Guards run_confirmation_pass() against overlap between the scheduled 5-min
# job and a manual "Confirm Now" click - without it, both could pass the
# "one signal per symbol per day" existence check for the same symbol before
# either commits, inserting two Signal rows and, in auto mode, placing two
# real orders for what should be one signal. A full pass can take minutes
# (rate-limit retries across the whole watchlist), so this isn't a
# theoretical race.
_confirmation_lock = threading.Lock()

# order_id -> consecutive failed get_ltp() calls in monitor_positions, so a
# struggling/unreachable broker connection escalates to a loud, visible
# alert instead of silently leaving a live position's stop-loss/target
# unchecked. Reset to 0 (and removed from _monitor_alerted) the moment a
# price read succeeds again.
_monitor_fail_counts: dict[int, int] = {}
_monitor_alerted: set[int] = set()
_MONITOR_FAIL_ALERT_THRESHOLD = 3  # consecutive minutes of failure before alerting


def send_webhook(settings: StrategySettings, event: str, payload: dict) -> tuple[bool, str]:
    """POSTs {event, ...payload} to the user-configured webhook URL, so the
    same strategy can feed any other algo platform / bot that accepts a
    generic JSON webhook. Best-effort: a broken webhook URL should never
    interrupt the strategy loop, just show up as a loud, clear error (same
    alert pipeline as everything else) so it doesn't fail silently.
    Returns (ok, message) - also used directly by the Settings tab's "Send
    Test Webhook" button.
    """
    if not settings.webhook_enabled or not settings.webhook_url:
        return False, "webhook is not enabled / no URL configured"
    body = {"event": event, "ts": dt.datetime.utcnow().isoformat(), **payload}
    try:
        resp = requests.post(settings.webhook_url, json=body, timeout=8)
        if not (200 <= resp.status_code < 300):
            msg = f"webhook POST to {settings.webhook_url} returned HTTP {resp.status_code}: {resp.text[:200]}"
            log.error(msg)
            return False, msg
        return True, f"HTTP {resp.status_code}"
    except Exception as e:
        msg = f"webhook POST to {settings.webhook_url} failed: {e}"
        log.error(msg)
        return False, msg


def _today_str() -> str:
    return dt.date.today().isoformat()


def load_persisted_state():
    """Restore the last scan/indicator results from the DB at startup, so a
    server restart doesn't blank the dashboard back to "no scan yet" while
    last_scan_ts (which does persist) keeps showing a real, recent time."""
    global _watchlist, _scan_detail, _indicator_status, _all_stocks, _index_reads
    with SessionLocal() as db:
        state = db.query(EngineState).first()
        if not state:
            return
        _watchlist = json.loads(state.watchlist_json or "[]")
        _scan_detail = json.loads(state.scan_detail_json or "{}")
        _indicator_status = json.loads(state.indicator_status_json or "{}")
        _all_stocks = json.loads(state.all_stocks_json or "[]")
        _index_reads = json.loads(state.index_reads_json or "{}")


def _persist_scan_state(db):
    state = db.query(EngineState).first()
    state.watchlist_json = json.dumps(_watchlist)
    state.scan_detail_json = json.dumps(_scan_detail)
    state.all_stocks_json = json.dumps(_all_stocks)
    state.index_reads_json = json.dumps(_index_reads)
    db.commit()


def _persist_indicator_state(db):
    state = db.query(EngineState).first()
    state.indicator_status_json = json.dumps(_indicator_status)
    db.commit()


def get_settings(db) -> StrategySettings:
    return db.query(StrategySettings).first()


def get_state(db) -> EngineState:
    global _watchlist, _scan_detail, _indicator_status, _all_stocks, _index_reads
    state = db.query(EngineState).first()
    if state.trading_day != _today_str():
        if state.trading_day:
            _snapshot_daily_summary(db, state)
        state.trading_day = _today_str()
        state.trades_today = 0
        state.realized_pnl_today = 0.0
        state.target_hit = False
        state.bias = ""
        # Clear all persisted scan/indicator state on day rollover so
        # yesterday's watchlist and reads don't linger alongside today's
        # blank bias until today's own scan runs.
        _watchlist, _all_stocks = [], []
        _scan_detail, _indicator_status, _index_reads = {}, {}, {}
        _persist_scan_state(db)
        _persist_indicator_state(db)
        db.commit()
    return state


def _snapshot_daily_summary(db, state: EngineState):
    """Called right before EngineState's daily counters reset, so
    yesterday's numbers land in DailySummary instead of just vanishing."""
    if db.query(DailySummary).filter(DailySummary.trading_day == state.trading_day).first():
        return  # already snapshotted (e.g. two restarts on the same stale day)
    day_signals = (
        db.query(Signal)
        .filter(Signal.ts >= dt.datetime.combine(dt.date.fromisoformat(state.trading_day), dt.time.min))
        .filter(Signal.ts < dt.datetime.combine(dt.date.fromisoformat(state.trading_day), dt.time.max))
        .all()
    )
    db.add(DailySummary(
        trading_day=state.trading_day,
        bias=state.bias,
        trades_taken=state.trades_today,
        realized_pnl=state.realized_pnl_today,
        target_hit=state.target_hit,
        paper_mode=state.paper_mode,
        signals_fired=len(day_signals),
        signals_target_hit=sum(1 for s in day_signals if s.outcome == "target_hit"),
        signals_sl_hit=sum(1 for s in day_signals if s.outcome == "sl_hit"),
    ))


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "exceeding access rate" in msg or "access denied" in msg or "too many requests" in msg


def _fetch_confirmation_candles(broker, symbol: str, token: str, from_dt: str, to_dt: str):
    """Candle fetch for the 5-min confirmation stack, with rate-limit
    backoff/retry matching scanner._fetch_day_change. Also retries if the
    broker returns fewer than the 30 candles the confirmation stack needs,
    since a short response is usually the same throttling rather than a
    genuine data gap. 5 attempts (not 3) specifically for rate-limit errors,
    since a heavy-throttling day can otherwise exhaust 3 quickly and still
    fail a symbol that would have come through on a 4th or 5th try."""
    max_attempts = 5
    last_exc = None
    for attempt in range(max_attempts):
        try:
            candles = broker.get_candles("NSE", symbol, token, "FIVE_MINUTE", from_dt, to_dt)
            if len(candles) >= 30 or attempt == max_attempts - 1:
                return candles
            log.warning("Short candle set for %s (%d rows), retrying (attempt %d/%d)",
                        symbol, len(candles), attempt + 1, max_attempts)
        except Exception as e:
            last_exc = e
            if not (_is_rate_limit_error(e) and attempt < max_attempts - 1):
                raise
            log.warning("Rate-limited fetching candles for %s, retrying (attempt %d/%d)",
                        symbol, attempt + 1, max_attempts)
        time.sleep(1.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return candles


def _first_connected_broker():
    """Any connected account's broker can serve as the shared market-data
    source for scanning - price data is the same regardless of which broker
    account happens to be logged in.
    """
    with SessionLocal() as db:
        accounts = db.query(Account).filter(Account.enabled == True).all()  # noqa: E712
    for acc in accounts:
        conn = broker_manager.get_connection(acc.id)
        if conn:
            return conn
    return None


def _compute_stop(direction: str, close: float, low: float, high: float,
                   sl_mode: str, sl_pct: float, sl_points: float) -> float:
    """Stop-loss price, per StrategySettings.sl_mode:
      "candle"       - breakout candle's low (long) / high (short) - the
                       original design-doc rule, moves with each candle.
      "fixed_pct"    - entry -/+ (entry x sl_pct / 100). Constant % move.
      "fixed_points" - entry -/+ sl_points. A flat Rs distance, not a %.
    """
    if direction == "BUY":
        if sl_mode == "fixed_pct":
            return round(close * (1 - sl_pct / 100), 2)
        if sl_mode == "fixed_points":
            return round(close - sl_points, 2)
        return round(low, 2)
    if sl_mode == "fixed_pct":
        return round(close * (1 + sl_pct / 100), 2)
    if sl_mode == "fixed_points":
        return round(close + sl_points, 2)
    return round(high, 2)


def compute_trade_levels(direction: str, close: float, low: float, high: float,
                          target_mode: str, r_multiple: float, target_pct: float,
                          sl_mode: str = "candle", sl_pct: float = 1.0, sl_points: float = 1.0
                          ) -> tuple[float, float, float]:
    """Concrete entry / target / stop-loss prices for a signal.

    Entry is the breakout candle's close. Stop loss and target are each
    independently configurable (StrategySettings.sl_mode / target_mode) -
    when target_mode="r_multiple", the target is derived from whatever stop
    distance sl_mode produced (stop distance x r_multiple), so changing the
    stop-loss mode changes the target too in that combination, by design.
    """
    entry = round(close, 2)
    stop = _compute_stop(direction, close, low, high, sl_mode, sl_pct, sl_points)
    if direction == "BUY":
        if target_mode == "fixed_pct":
            target = round(close * (1 + target_pct / 100), 2)
        else:
            risk = max(close - stop, 0.0)
            target = round(close + risk * r_multiple, 2)
        return entry, target, stop
    if target_mode == "fixed_pct":
        target = round(close * (1 - target_pct / 100), 2)
    else:
        risk = max(stop - close, 0.0)
        target = round(close - risk * r_multiple, 2)
    return entry, target, stop


def resubscribe_live_feed(broker):
    """Re-arm the live WebSocket subscription for whatever's already in
    _all_stocks/_index_reads (restored from persisted state, or scanned
    earlier today). A fresh broker connection starts with an empty
    subscription set, so without this prices stay frozen at the last scan
    until the next full scan runs."""
    subscribe_live = getattr(broker, "subscribe_live", None)
    if not subscribe_live:
        return
    tokens = [s["token"] for s in _all_stocks if s.get("token")]
    tokens += [r["token"] for r in _index_reads.values() if r.get("token")]
    if tokens:
        subscribe_live(tokens)


def _run_full_scan(update_watchlist: bool, label: str):
    """Shared implementation behind the official 9:30 scan and the
    pre-/post-market informational snapshots - same index/sector/stock
    scan either way, but only the official scan (update_watchlist=True) is
    allowed to change what the confirmation pass and execution act on."""
    global _watchlist, _scan_detail, _all_stocks, _index_reads
    broker = _first_connected_broker()
    if broker is None:
        log.error("No connected broker account - skipping %s. Check the Broker Accounts panel.", label)
        scanner._progress["label"] = "idle"
        return None

    with SessionLocal() as db:
        settings = get_settings(db)
        sector_threshold = settings.sector_move_threshold_pct
        stock_threshold = settings.stock_move_threshold_pct

    bias_result = scanner.scan_index_bias(broker)
    scan_result = scanner.scan_sectors_and_stocks(broker, bias_result["overall"], sector_threshold, stock_threshold)
    if update_watchlist:
        _watchlist = scan_result["watchlist"]
    _all_stocks = scan_result["all_stocks"]
    _index_reads = bias_result["indices"]
    _scan_detail = {
        "indices": bias_result["indices"],
        "sector_moves": scan_result["sector_moves"],
        "gainers": scan_result["gainers"],
        "losers": scan_result["losers"],
    }

    resubscribe_live_feed(broker)

    with SessionLocal() as db:
        state = get_state(db)
        state.bias = bias_result["overall"]
        state.last_scan_ts = dt.datetime.utcnow()
        db.commit()
        _persist_scan_state(db)

    scanner._progress["label"] = "idle"
    log.info("%s complete: bias=%s watchlist=%d symbols", label, bias_result["overall"], len(_watchlist))
    return {"bias": bias_result, "watchlist": _watchlist, **_scan_detail}


def run_market_scan():
    """Index bias + sector/stock scan. Fires once at the configured scan
    time and populates the trading watchlist."""
    return _run_full_scan(update_watchlist=True, label="Scan")


def run_premarket_snapshot():
    """Informational-only refresh of index bias / gainers / losers / sector
    ranking before market open. Does NOT touch the trading watchlist -
    avoids acting on noisy pre-open prices, so this is display-only."""
    return _run_full_scan(update_watchlist=False, label="Pre-market snapshot")


def run_postmarket_snapshot():
    """Informational-only refresh after market close, so the dashboard
    reflects real closing prices instead of freezing at whatever the last
    tick happened to be a few minutes before close. The trading day is
    already over (square-off already ran), so this never touches the
    watchlist either."""
    return _run_full_scan(update_watchlist=False, label="Post-market snapshot")


def retry_unknown_indices():
    """Retries any index that got rate-limited during the once-a-day 9:30
    scan and is still showing "no data". Runs every 5 minutes piggy-backed
    on the confirmation pass below, which fires on that cadence regardless
    of watchlist state."""
    if not _index_reads:
        return
    stale = {name: r for name, r in _index_reads.items() if r.get("bias") == "unknown"}
    if not stale:
        return
    broker = _first_connected_broker()
    if broker is None:
        return
    universe = scanner.load_universe()
    for name in stale:
        meta = universe["indices"].get(name)
        if not meta:
            continue
        token = stale[name].get("token") or _token_for(meta["symbol"])
        result = scanner._fetch_day_change(broker, meta["exchange"], meta["symbol"], token)
        if result is None:
            continue
        ltp, chg, day_open = result
        _index_reads[name] = {"bias": "bullish" if chg > 0 else "bearish", "change_pct": chg,
                               "ltp": ltp, "token": token, "day_open": day_open}
        log.info("Recovered index data for %s after earlier rate-limit", name)
    with SessionLocal() as db:
        _persist_scan_state(db)


def run_confirmation_pass():
    """Step 3: run the 5-indicator stack on every watchlist symbol on the
    5-min chart. Firing signals become Signal rows - pending in manual mode,
    auto-executed in auto mode.
    """
    if not _confirmation_lock.acquire(blocking=False):
        log.warning("Confirmation pass already running - skipped this trigger to avoid duplicate signals/orders.")
        return []
    try:
        return _run_confirmation_pass_locked()
    finally:
        _confirmation_lock.release()


def _run_confirmation_pass_locked():
    retry_unknown_indices()
    if not _watchlist:
        return []
    broker = _first_connected_broker()
    if broker is None:
        log.error("No connected broker account - confirmation pass skipped, indicators will not update.")
        return []

    with SessionLocal() as db:
        state = get_state(db)
        if state.target_hit:
            return []
        run_mode = state.run_mode
        settings = get_settings(db)
        enabled_indicators = {
            "ema": settings.use_ema, "vwap": settings.use_vwap, "supertrend": settings.use_supertrend,
            "rsi": settings.use_rsi, "adx": settings.use_adx, "volume": settings.use_volume,
        }

    now = dt.datetime.now()
    from_dt = (now - dt.timedelta(days=2)).strftime("%Y-%m-%d 09:15")
    to_dt = now.strftime("%Y-%m-%d %H:%M")

    fired = []
    for item in _watchlist:
        try:
            candles = _fetch_confirmation_candles(broker, item["symbol"], item["token"], from_dt, to_dt)
        except Exception as e:
            # A rate limit that survives 5 retries is still just the broker
            # being busy - the next confirmation pass (5 minutes away) tries
            # again and usually succeeds, so this isn't worth an alarm sound
            # for something that isn't actually broken. Anything else
            # (a real API error, invalid token, etc.) stays loud.
            if _is_rate_limit_error(e):
                log.warning("Candle fetch rate-limited for %s after retries, will retry next cycle: %s",
                            item["symbol"], e)
            else:
                log.error("Candle fetch failed for %s after retries: %s", item["symbol"], e)
            time.sleep(0.6)
            continue

        result = evaluate_confirmation_stack(candles, enabled_indicators)
        if result.get("reads") is None:
            log.warning("No indicator read for %s: %s (%d candles)",
                        item["symbol"], result.get("reason", "unknown"), len(candles))
        _indicator_status[item["symbol"]] = {
            "reads": result.get("reads"),
            "aligned": result.get("aligned"),
            "sector": item["sector"],
            "direction": item["direction"],
            "ts": dt.datetime.utcnow().isoformat(),
        }
        time.sleep(0.6)

        wanted = "BUY" if item["direction"] == "bullish" else "SELL"
        if result["aligned"] != wanted:
            continue  # mixed read, or confirmation disagrees with the sector/stock scan -> skip

        with SessionLocal() as db:
            # One signal per symbol per day, regardless of its final status
            # (executed, expired, or rejected) - prevents duplicate signals
            # firing for the same symbol later in the day.
            existing = (
                db.query(Signal)
                .filter(Signal.symbol == item["symbol"])
                .filter(Signal.ts >= dt.datetime.combine(dt.date.today(), dt.time.min))
                .first()
            )
            if existing:
                continue
            trig = result["trigger_candle"]
            settings = get_settings(db)
            entry, target, stop = compute_trade_levels(
                wanted, trig["close"], trig["low"], trig["high"],
                settings.target_mode, settings.target_r_multiple, settings.target_pct,
                settings.sl_mode, settings.sl_pct, settings.sl_points,
            )
            signal = Signal(
                symbol=item["symbol"], sector=item["sector"], direction=wanted,
                trigger_low=trig["low"], trigger_high=trig["high"],
                entry_price=entry, target_price=target, stop_loss_price=stop,
                status="pending", detail=str(result["reads"]),
            )
            db.add(signal)
            db.commit()
            db.refresh(signal)
            fired.append(signal.id)
            send_webhook(settings, "signal_fired", {
                "signal_id": signal.id, "symbol": signal.symbol, "sector": signal.sector,
                "direction": signal.direction, "entry_price": signal.entry_price,
                "target_price": signal.target_price, "stop_loss_price": signal.stop_loss_price,
            })

        if run_mode == "auto":
            execute_signal(signal.id)

    with SessionLocal() as db:
        _persist_indicator_state(db)

    return fired


def _risk_check(db, state: EngineState, settings: StrategySettings) -> tuple[bool, str]:
    # Max-trades-per-day is checked per account (Account.max_trades_per_day)
    # inside execute_signal's per-account loop, since each account can have
    # a different cap.
    if state.target_hit:
        return False, "daily profit target already reached"
    now = dt.datetime.now().time()
    if now >= dt.time.fromisoformat(settings.squareoff_time):
        return False, "past square-off time, no new entries"
    return True, ""


def _account_trades_today(db, account_id: int) -> int:
    start = dt.datetime.combine(dt.date.today(), dt.time.min)
    return db.query(Order).filter(Order.account_id == account_id, Order.ts >= start).count()


def _account_capital_for_trade(account: Account, funds: float) -> float:
    if account.sizing_mode == "fixed_amount":
        # Only rejected negative values at input time (see accounts.py's
        # _validate_sizing) - nothing stopped a fixed amount larger than the
        # account can actually afford. Clamp to real available funds so a
        # misconfigured cap can't fabricate unrealistic paper P&L (simulating
        # a trade the account could never afford) or, in live mode, attempt
        # to size a real order beyond what's actually available.
        return min(account.fixed_amount_per_trade, funds)
    return funds * (account.capital_per_trade_pct / 100)


def execute_signal(signal_id: int):
    with SessionLocal() as db:
        signal = db.get(Signal, signal_id)
        if not signal or signal.status not in ("pending", "approved"):
            return
        state = get_state(db)
        settings = get_settings(db)
        ok, reason = _risk_check(db, state, settings)
        if not ok:
            signal.status = "expired"
            signal.detail += f" | blocked: {reason}"
            db.commit()
            return

        global_paper_mode = state.paper_mode
        accounts = db.query(Account).filter(Account.enabled == True).all()  # noqa: E712
        placed_any = False
        for account in accounts:
            conn = broker_manager.get_connection(account.id)
            if not conn:
                continue
            trades_today = _account_trades_today(db, account.id)
            if trades_today >= account.max_trades_per_day:
                log.warning("Order skipped for %s / %s: account already hit its max_trades_per_day (%d/%d)",
                            account.label, signal.symbol, trades_today, account.max_trades_per_day)
                continue
            # An account marked "sandbox" always simulates, regardless of the
            # global paper/live toggle - it's a per-account safety floor, not
            # something the global switch can override into placing real
            # orders on it.
            paper_mode = global_paper_mode or account.mode == "sandbox"
            try:
                token = _token_for(signal.symbol)
                funds = conn.get_available_funds()
                ltp = conn.get_ltp("NSE", signal.symbol, token)
                capital = _account_capital_for_trade(account, funds)
                qty = max(int(capital // ltp), 0)
                if qty <= 0:
                    sizing_desc = (f"fixed Rs {account.fixed_amount_per_trade}" if account.sizing_mode == "fixed_amount"
                                    else f"{account.capital_per_trade_pct:.0f}% of funds (Rs {funds:.2f})")
                    log.error(
                        "Order skipped for %s / %s: %s = Rs %.2f, can't buy even 1 share at Rs %.2f - "
                        "add funds or adjust this account's sizing in the Accounts tab",
                        account.label, signal.symbol, sizing_desc, capital, ltp,
                    )
                    continue
                if paper_mode:
                    # Simulated fill - the broker connection is used only as
                    # a market-data source here, never to place a real
                    # order. Triggered by the global switch OR this
                    # account's own "sandbox" mode (see above).
                    order_id = f"PAPER-{signal.id}-{account.id}"
                else:
                    order_id = conn.place_order("NSE", signal.symbol, token, signal.direction, qty)
                order = Order(
                    account_id=account.id, signal_id=signal.id, symbol=signal.symbol,
                    side=signal.direction, quantity=qty, price=ltp,
                    mode="paper" if paper_mode else "live",
                    status="open", stop_loss=signal.stop_loss_price, target_price=signal.target_price,
                    broker_order_id=str(order_id),
                )
                db.add(order)
                placed_any = True
                send_webhook(settings, "order_executed", {
                    "account_id": account.id, "account_label": account.label,
                    "symbol": signal.symbol, "side": signal.direction, "quantity": qty,
                    "price": ltp, "mode": "paper" if paper_mode else "live",
                    "stop_loss": signal.stop_loss_price, "target_price": signal.target_price,
                    "broker_order_id": str(order_id),
                })
            except Exception as e:
                log.error("Order failed for account %s / %s: %s", account.label, signal.symbol, e)

        if placed_any:
            signal.status = "executed"
            signal.executed = True
            state.trades_today += 1
        else:
            signal.status = "expired"
            signal.detail += " | no account could fill this order"
            log.error("Signal %s (%s %s) expired - connected account(s) couldn't fill it "
                      "(insufficient funds/quantity, max trades reached, or an order error - see preceding log line)",
                      signal.id, signal.direction, signal.symbol)
        db.commit()


def reject_signal(signal_id: int):
    with SessionLocal() as db:
        signal = db.get(Signal, signal_id)
        if signal and signal.status == "pending":
            signal.status = "rejected"
            db.commit()


def monitor_positions():
    """Checks open positions against stop-loss AND target, and rolls up
    realized P&L against the 5% daily target (step 07 fund & risk rules)."""
    with SessionLocal() as db:
        state = get_state(db)
        settings = get_settings(db)
        open_orders = db.query(Order).filter(Order.status == "open").all()
        for order in open_orders:
            conn = broker_manager.get_connection(order.account_id)
            if not conn:
                continue
            try:
                ltp = conn.get_ltp("NSE", order.symbol, _token_for(order.symbol))
                _monitor_fail_counts.pop(order.id, None)
                _monitor_alerted.discard(order.id)
            except Exception as e:
                fails = _monitor_fail_counts.get(order.id, 0) + 1
                _monitor_fail_counts[order.id] = fails
                if fails >= _MONITOR_FAIL_ALERT_THRESHOLD and order.id not in _monitor_alerted:
                    _monitor_alerted.add(order.id)
                    msg = (f"Stop-loss/target monitoring for {order.symbol} (order {order.id}, account "
                           f"{order.account_id}) has failed {fails} checks in a row - this position is "
                           f"NOT being watched right now. Last error: {e}")
                    log.error(msg)
                    send_webhook(settings, "position_monitoring_stale", {
                        "order_id": order.id, "account_id": order.account_id,
                        "symbol": order.symbol, "consecutive_failures": fails, "error": str(e),
                    })
                continue

            stop_hit = (
                (order.side == "BUY" and ltp <= order.stop_loss) or
                (order.side == "SELL" and ltp >= order.stop_loss)
            )
            target_hit = order.target_price and (
                (order.side == "BUY" and ltp >= order.target_price) or
                (order.side == "SELL" and ltp <= order.target_price)
            )
            if stop_hit:
                _close_order(db, conn, order, ltp, "stop loss hit", outcome="sl_hit")
            elif target_hit:
                _close_order(db, conn, order, ltp, "target hit", outcome="target_hit")

        db.commit()
        _track_unexecuted_signal_outcomes(db)
        db.commit()

        total_capital = sum(
            broker_manager.get_connection(a.id).get_available_funds()
            for a in db.query(Account).filter(Account.enabled == True).all()  # noqa: E712
            if broker_manager.get_connection(a.id)
        )
        # If every connected account reports 0 funds (broker glitch, funds
        # not fetched yet, or a genuinely empty account), there's no
        # meaningful capital base to measure the daily target against - the
        # old `or 1.0` fallback compared P&L to a fraction of a rupee, so
        # any nonzero profit instantly halted trading for the rest of the
        # day with no explanation. Skip the check instead until real
        # capital data is available.
        if total_capital > 0 and state.realized_pnl_today >= total_capital * (settings.daily_profit_target_pct / 100):
            state.target_hit = True
            db.commit()


def _track_unexecuted_signal_outcomes(db):
    """Even a signal that never got approved/executed still had a real
    entry/target/stop - tracking what would have happened lets the
    strategy be reviewed on its own merits, not just on the trades that
    happened to be taken."""
    broker = _first_connected_broker()
    if broker is None:
        return
    open_signals = (
        db.query(Signal)
        .filter(Signal.outcome == "open", Signal.executed == False)  # noqa: E712
        .filter(Signal.ts >= dt.datetime.combine(dt.date.today(), dt.time.min))
        .all()
    )
    for signal in open_signals:
        if not (signal.entry_price and signal.target_price and signal.stop_loss_price):
            continue
        try:
            ltp = broker.get_ltp("NSE", signal.symbol, _token_for(signal.symbol))
        except Exception:
            continue
        stop_hit = (
            (signal.direction == "BUY" and ltp <= signal.stop_loss_price) or
            (signal.direction == "SELL" and ltp >= signal.stop_loss_price)
        )
        target_hit = (
            (signal.direction == "BUY" and ltp >= signal.target_price) or
            (signal.direction == "SELL" and ltp <= signal.target_price)
        )
        if not (stop_hit or target_hit):
            continue
        direction = 1 if signal.direction == "BUY" else -1
        signal.outcome = "sl_hit" if stop_hit else "target_hit"
        signal.outcome_price = ltp
        signal.outcome_ts = dt.datetime.utcnow()
        signal.outcome_pnl_pct = round((ltp - signal.entry_price) / signal.entry_price * 100 * direction, 2)


def _close_order(db, conn, order: Order, exit_price: float, reason: str, outcome: str = "squared_off"):
    if order.mode != "paper":
        opposite = "SELL" if order.side == "BUY" else "BUY"
        try:
            conn.place_order("NSE", order.symbol, _token_for(order.symbol), opposite, order.quantity)
        except Exception as e:
            # order.status stays "open" so this position is picked up again
            # by the next square_off_all retry pass (see scheduler.py's
            # square-off retry window) instead of being silently abandoned
            # open past close time.
            msg = (f"Exit order FAILED for {order.symbol} (order {order.id}, account {order.account_id}, "
                   f"{reason}): {e} - position is still open and will be retried.")
            log.error(msg)
            with SessionLocal() as alert_db:
                settings = get_settings(alert_db)
                send_webhook(settings, "squareoff_failed", {
                    "order_id": order.id, "account_id": order.account_id,
                    "symbol": order.symbol, "reason": reason, "error": str(e),
                })
            return
    order.status = "closed"
    order.exit_price = exit_price
    order.exit_ts = dt.datetime.utcnow()
    direction = 1 if order.side == "BUY" else -1
    order.pnl = (exit_price - order.price) * order.quantity * direction
    order.note = reason
    state = get_state(db)
    state.realized_pnl_today += order.pnl

    if order.signal_id:
        signal = db.get(Signal, order.signal_id)
        if signal and signal.outcome == "open":
            signal.outcome = outcome
            signal.outcome_price = exit_price
            signal.outcome_ts = order.exit_ts
            if signal.entry_price:
                signal.outcome_pnl_pct = round((exit_price - signal.entry_price) / signal.entry_price * 100 * direction, 2)


def square_off_all():
    """3:15 PM - close every open intraday position, no new entries after."""
    with SessionLocal() as db:
        open_orders = db.query(Order).filter(Order.status == "open").all()
        for order in open_orders:
            conn = broker_manager.get_connection(order.account_id)
            if not conn:
                continue
            try:
                ltp = conn.get_ltp("NSE", order.symbol, _token_for(order.symbol))
            except Exception:
                continue
            _close_order(db, conn, order, ltp, "3:15 square-off", outcome="squared_off")

        # Unexecuted signals still sitting "open" at square-off - mark them
        # closed out too using whatever the last known price was, so
        # nothing is left in limbo for tomorrow's review.
        stale_signals = (
            db.query(Signal)
            .filter(Signal.outcome == "open", Signal.executed == False)  # noqa: E712
            .filter(Signal.ts >= dt.datetime.combine(dt.date.today(), dt.time.min))
            .all()
        )
        broker = _first_connected_broker()
        for signal in stale_signals:
            if not (broker and signal.entry_price):
                continue
            try:
                ltp = broker.get_ltp("NSE", signal.symbol, _token_for(signal.symbol))
            except Exception:
                continue
            direction = 1 if signal.direction == "BUY" else -1
            signal.outcome = "squared_off"
            signal.outcome_price = ltp
            signal.outcome_ts = dt.datetime.utcnow()
            signal.outcome_pnl_pct = round((ltp - signal.entry_price) / signal.entry_price * 100 * direction, 2)

        db.commit()


def get_watchlist():
    """Watchlist rows with change_pct refreshed from the live tick feed
    where available, same as get_live_scan_detail()."""
    broker = _first_connected_broker()
    get_live_quote = getattr(broker, "get_live_quote", None) if broker else None
    if not get_live_quote:
        return _watchlist

    all_by_token = {s["token"]: s for s in _all_stocks if s.get("token")}
    updated = []
    for item in _watchlist:
        tick = get_live_quote(item["token"]) if item.get("token") else None
        if tick and "ltp" in tick:
            cached = all_by_token.get(item["token"], {})
            day_open = tick.get("open") or cached.get("day_open")
            entry = dict(item)
            if day_open:
                entry["change_pct"] = round((tick["ltp"] - day_open) / day_open * 100, 2)
            updated.append(entry)
        else:
            updated.append(item)
    return updated


def get_scan_detail():
    return _scan_detail


def get_live_scan_detail():
    """Same shape as get_scan_detail(), but overlays instant prices from the
    broker's WebSocket tick cache (see angel_one_ws.py) wherever available,
    so gainers/losers/sector ranking/index cards update in real time between
    full scans - without re-hitting the REST API (which is what rate-limited
    us when this was polled repeatedly)."""
    broker = _first_connected_broker()
    get_live_quote = getattr(broker, "get_live_quote", None) if broker else None
    if not get_live_quote:
        return _scan_detail

    def _overlay(entry: dict) -> dict:
        tick = get_live_quote(entry["token"]) if entry.get("token") else None
        if not tick or "ltp" not in tick:
            return entry
        day_open = tick.get("open") or entry.get("day_open")
        updated = dict(entry, ltp=tick["ltp"])
        if day_open:
            updated["change_pct"] = round((tick["ltp"] - day_open) / day_open * 100, 2)
        return updated

    live_indices = {}
    for name, r in _index_reads.items():
        if r.get("bias") == "unknown":
            live_indices[name] = r
            continue
        updated = _overlay(r)
        updated["bias"] = "bullish" if updated["change_pct"] > 0 else "bearish"
        live_indices[name] = updated

    live_stocks = [_overlay(s) for s in _all_stocks]
    sector_totals: dict[str, list[float]] = {}
    for s in live_stocks:
        sector_totals.setdefault(s["sector"], []).append(s["change_pct"])
    sector_moves = {sector: round(sum(vals) / len(vals), 2) for sector, vals in sector_totals.items()}

    gainers = sorted([s for s in live_stocks if s["change_pct"] > 0], key=lambda s: -s["change_pct"])[:15]
    losers = sorted([s for s in live_stocks if s["change_pct"] < 0], key=lambda s: s["change_pct"])[:15]

    return {"indices": live_indices, "sector_moves": sector_moves, "gainers": gainers, "losers": losers}


def get_indicator_status():
    return _indicator_status


def get_symbol_candles(symbol: str, limit: int = 30):
    """On-demand 5-min candle fetch for the dashboard's candle viewer -
    not part of the scheduled scan, so no throttling needed for a single call."""
    broker = _first_connected_broker()
    if broker is None:
        raise RuntimeError("No connected broker account")
    token = _token_for(symbol)
    now = dt.datetime.now()
    from_dt = (now - dt.timedelta(days=2)).strftime("%Y-%m-%d 09:15")
    to_dt = now.strftime("%Y-%m-%d %H:%M")
    candles = broker.get_candles("NSE", symbol, token, "FIVE_MINUTE", from_dt, to_dt)
    rows = candles.tail(limit).to_dict("records")
    for row in rows:
        row["ts"] = str(row["ts"])
    return rows
