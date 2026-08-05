"""Resolves NSE trading symbols -> instrument tokens using Angel One's public
scrip master (no login required). Used by every broker adapter that needs a
symboltoken. Instrument tokens can change day to day (delisting, symbol
changes, new listings), so this re-downloads once per calendar day rather
than trusting an old cached copy indefinitely - cached only for the trading
day, not kept around across days.
"""
import datetime as dt
import json
import logging
import requests
from . import config

log = logging.getLogger("instrument_master")

SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_PATH = config.DATA_DIR / "scrip_master.json"

_symbol_index: dict[str, dict] = {}
_cached_for_day: str = ""  # date the in-memory index was built for (YYYY-MM-DD)


def _today_str() -> str:
    return dt.date.today().isoformat()


def _cache_is_from_today() -> bool:
    if not CACHE_PATH.exists():
        return False
    cached_date = dt.date.fromtimestamp(CACHE_PATH.stat().st_mtime).isoformat()
    return cached_date == _today_str()


def _download() -> list[dict]:
    log.info("Downloading fresh scrip master (daily refresh)")
    resp = requests.get(SCRIP_MASTER_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    CACHE_PATH.write_text(json.dumps(data))
    return data


def _load() -> list[dict]:
    if _cache_is_from_today():
        return json.loads(CACHE_PATH.read_text())
    return _download()


def _build_index(data: list[dict]) -> dict[str, dict]:
    idx = {}
    for row in data:
        exch = row.get("exch_seg", "")
        symbol = row.get("symbol", "")
        name = row.get("name", "")
        key = f"{exch}:{symbol}"
        idx[key] = row
        # also index by name for indices like "Nifty 50"
        idx.setdefault(f"{exch}:NAME:{name}", row)
    return idx


def _ensure_index():
    global _symbol_index, _cached_for_day
    if _symbol_index and _cached_for_day == _today_str():
        return
    _symbol_index = _build_index(_load())
    _cached_for_day = _today_str()


# Old signals / watchlists may still carry pre-rebrand NSE symbols.
_SYMBOL_ALIASES = {
    "TATAMOTORS": "TMPV",
    "TATAMOTORS-EQ": "TMPV-EQ",
}


def resolve(exchange: str, tradingsymbol: str) -> dict | None:
    """Returns the raw scrip row (has 'token', 'symbol', 'name', 'lotsize', ...).

    Tries exact match, then -EQ suffix for NSE cash equities, then known
    rebrand aliases (e.g. TATAMOTORS -> TMPV). Returns None if unresolved.
    """
    _ensure_index()
    if not tradingsymbol:
        return None

    candidates = [tradingsymbol]
    if exchange == "NSE" and not tradingsymbol.endswith("-EQ"):
        candidates.append(f"{tradingsymbol}-EQ")
    alias = _SYMBOL_ALIASES.get(tradingsymbol) or _SYMBOL_ALIASES.get(tradingsymbol.upper())
    if alias:
        candidates.append(alias)
        if exchange == "NSE" and not alias.endswith("-EQ"):
            candidates.append(f"{alias}-EQ")

    seen: set[str] = set()
    for sym in candidates:
        if sym in seen:
            continue
        seen.add(sym)
        hit = _symbol_index.get(f"{exchange}:{sym}")
        if hit:
            return hit
    return _symbol_index.get(f"{exchange}:NAME:{tradingsymbol.upper()}")


def refresh():
    """Force a fresh download regardless of today's cache state - wired to
    the Settings tab's "Refresh Master Data" button for a manual redo."""
    global _symbol_index, _cached_for_day
    _symbol_index = _build_index(_download())
    _cached_for_day = _today_str()


def ensure_fresh_for_today():
    """Called once at server startup so the day's first download (and any
    network failure) happens eagerly and visibly in the logs, rather than
    lazily inside whatever request happens to call resolve() first."""
    _ensure_index()
