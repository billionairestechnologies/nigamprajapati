import json
import time
import logging
import datetime as dt
from pathlib import Path
from .. import instrument_master

log = logging.getLogger("scanner")

UNIVERSE_PATH = Path(__file__).parent / "universe.json"

# Angel One's SmartAPI (and brokers in general) rate-limit historical/LTP
# calls to a few requests/second - without pacing, a full sector+stock scan
# (7 indices + ~60 stocks, 2 calls each) trips those limits partway through
# and silently drops the symbols that got throttled.
_INTER_CALL_SECONDS = 0.4    # gap between the LTP call and the candle call for one symbol
_INTER_SYMBOL_SECONDS = 0.9  # gap before moving to the next symbol
_RATE_LIMIT_RETRY_SECONDS = 2.5


def load_universe() -> dict:
    return json.loads(UNIVERSE_PATH.read_text())


# Scan progress, exposed via /api/engine/scan-progress - a full scan takes
# 2-3 minutes because of the rate-limit-safe pacing above.
_progress = {"current": 0, "total": 0, "label": "idle"}


def get_progress() -> dict:
    return dict(_progress)


def _reset_progress(total: int, label: str):
    _progress["current"] = 0
    _progress["total"] = total
    _progress["label"] = label


def _tick_progress():
    _progress["current"] += 1


def _pct_change(quote_open: float, ltp: float) -> float:
    if not quote_open:
        return 0.0
    return (ltp - quote_open) / quote_open * 100


def _is_rate_limit_error(exc: Exception) -> bool:
    return "exceeding access rate" in str(exc).lower() or "access denied" in str(exc).lower()


def _fetch_day_change(broker, exchange: str, symbol: str, token: str) -> float | None:
    """LTP + previous day's open -> % change, with retries (escalating
    backoff) if the broker throttles us. Returns None (and logs) if the
    symbol still can't be read after that."""
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            ltp = broker.get_ltp(exchange, symbol, token)
            time.sleep(_INTER_CALL_SECONDS)
            candles = broker.get_candles(
                exchange, symbol, token, "ONE_DAY",
                (dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d 09:15"),
                dt.date.today().strftime("%Y-%m-%d 09:30"),
            )
            day_open = float(candles.iloc[-1]["open"]) if len(candles) else ltp
            return ltp, round(_pct_change(day_open, ltp), 2), day_open
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < max_attempts - 1:
                backoff = _RATE_LIMIT_RETRY_SECONDS * (attempt + 1)
                log.warning("Rate-limited on %s, retrying in %.1fs (attempt %d/%d)",
                            symbol, backoff, attempt + 1, max_attempts)
                time.sleep(backoff)
                continue
            log.warning("Scan fetch failed for %s: %s", symbol, e)
            return None
    return None


def scan_index_bias(broker) -> dict:
    """Step 1: scan the 7 indices for trend direction -> overall market bias."""
    universe = load_universe()
    reads = {}
    _reset_progress(len(universe["indices"]), "Scanning indices")
    for name, meta in universe["indices"].items():
        row = instrument_master.resolve(meta["exchange"], meta["symbol"])
        token = row["token"] if row else ""
        result = _fetch_day_change(broker, meta["exchange"], meta["symbol"], token)
        if result is None:
            reads[name] = {"bias": "unknown", "error": "fetch failed - see server logs", "token": token}
        else:
            ltp, chg, day_open = result
            reads[name] = {"bias": "bullish" if chg > 0 else "bearish", "change_pct": chg,
                            "ltp": ltp, "token": token, "day_open": day_open}
        _tick_progress()
        time.sleep(_INTER_SYMBOL_SECONDS)

    bull = sum(1 for r in reads.values() if r.get("bias") == "bullish")
    bear = sum(1 for r in reads.values() if r.get("bias") == "bearish")
    overall = "bullish" if bull > bear else ("bearish" if bear > bull else "mixed")
    return {"overall": overall, "indices": reads}


def scan_sectors_and_stocks(broker, bias: str, sector_move_threshold_pct: float,
                            stock_move_threshold_pct: float) -> dict:
    """Step 2: rank sectors by move, flag sectors moving at/above the given
    threshold, then pull individual stocks within those sectors moving
    at/above the stock threshold. This builds the watchlist passed to the
    5-min confirmation step. Thresholds are user-configurable (Settings
    tab), not hardcoded - see database.StrategySettings.

    Returns every scanned stock's move too (not just the filtered watchlist)
    so the dashboard can show a live top-gainers/top-losers board.
    """
    universe = load_universe()
    sector_moves = {}
    all_stocks = []

    total_stocks = sum(len(symbols) for symbols in universe["sectors"].values())
    _reset_progress(total_stocks, "Scanning sectors & stocks")
    for sector, symbols in universe["sectors"].items():
        moves = []
        for sym in symbols:
            row = instrument_master.resolve("NSE", sym)
            token = row["token"] if row else ""
            result = _fetch_day_change(broker, "NSE", sym, token)
            _tick_progress()
            time.sleep(_INTER_SYMBOL_SECONDS)
            if result is None:
                continue
            ltp, chg, day_open = result
            move = {"symbol": sym, "token": token, "sector": sector, "ltp": ltp,
                     "change_pct": chg, "day_open": day_open}
            moves.append(move)
            all_stocks.append(move)
        if moves:
            sector_moves[sector] = round(sum(m["change_pct"] for m in moves) / len(moves), 2)
            universe["sectors"][sector] = moves  # stash for reuse below

    leading = {
        s: avg for s, avg in sector_moves.items()
        if abs(avg) >= sector_move_threshold_pct
        and ((avg > 0 and bias != "bearish") or (avg < 0 and bias != "bullish"))
    }

    watchlist = []
    for sector in leading:
        for stock in universe["sectors"][sector]:
            if abs(stock["change_pct"]) >= stock_move_threshold_pct:
                watchlist.append({
                    "sector": sector,
                    "symbol": stock["symbol"],
                    "token": stock["token"],
                    "change_pct": stock["change_pct"],
                    "direction": "bullish" if stock["change_pct"] > 0 else "bearish",
                })

    gainers = sorted([s for s in all_stocks if s["change_pct"] > 0], key=lambda s: -s["change_pct"])[:15]
    losers = sorted([s for s in all_stocks if s["change_pct"] < 0], key=lambda s: s["change_pct"])[:15]

    return {
        "watchlist": watchlist,
        "sector_moves": sector_moves,
        "gainers": gainers,
        "losers": losers,
        "all_stocks": all_stocks,
    }
