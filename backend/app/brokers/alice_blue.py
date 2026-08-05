import datetime as dt
import logging
import pandas as pd
import pytz
import httpx
from .base import BrokerAdapter, BrokerError
from .alice_blue_ws import AliceBlueFeed

log = logging.getLogger("alice_blue")

REST_BASE = "https://a3.aliceblueonline.com"
HISTORICAL_URL = "https://a3.aliceblueonline.com/open-api/od/ChartAPIService/api/chart/history"
IST = pytz.timezone("Asia/Kolkata")


class AliceBlueBroker(BrokerAdapter):
    """Wraps Alice Blue's current "ANT" / V2 vendor API.

    Unlike Angel One, this can't log in with just a client id + password -
    Alice Blue's app model is OAuth-style: the user is redirected to
    https://ant.aliceblueonline.com/?appcode=... to log in, and Alice Blue
    redirects back with an authCode that we exchange (together with our app
    secret) for a session token. That exchange happens once, outside this
    class (see routers/accounts.py's /alice_blue/login and /callback routes)
    - this class is handed the resulting session_token and just uses it.

    There's no plain REST "get me the LTP" call in this API - live prices
    only come over a WebSocket feed, so get_ltp subscribes lazily and reads
    from a small in-memory cache (see alice_blue_ws.py).
    """

    name = "alice_blue"

    def __init__(self, client_id: str, session_token: str):
        self.client_id = client_id
        self.session_token = session_token
        self._feed: AliceBlueFeed | None = None

    def _headers(self):
        return {"Authorization": f"Bearer {self.session_token}", "Content-Type": "application/json"}

    def connect(self) -> None:
        if not self.session_token:
            raise BrokerError(
                "Alice Blue is not logged in yet - use the 'Login with Alice Blue' "
                "button on this account to complete the redirect flow first."
            )
        # Smoke-test the session token before trusting it.
        self.get_available_funds()
        try:
            self._feed = AliceBlueFeed(self.client_id, self.session_token)
            self._feed.start()
        except Exception as e:
            # Same tolerance as Angel One: live streaming is a nice-to-have
            # for the dashboard - scanning/trading still work over REST
            # (get_ltp falls back to a blocking subscribe) even if the feed
            # can't come up, so don't fail the whole connect over it.
            log.warning("Alice Blue connected but live feed failed to start: %s", e)
            self._feed = None

    def get_ltp(self, exchange: str, symbol: str, token: str) -> float:
        if not self._feed:
            raise BrokerError("Alice Blue feed not connected")
        quote = self._feed.subscribe_and_wait(exchange, token)
        if not quote or "ltp" not in quote:
            raise BrokerError(f"Alice Blue LTP not available yet for {symbol}")
        return float(quote["ltp"])

    def get_live_quote(self, token: str) -> dict | None:
        """Instant cached tick, same contract as AngelOneBroker.get_live_quote
        - lets the dashboard overlay live prices without hitting the REST/
        history endpoints between full scans."""
        if not self._feed:
            return None
        return self._feed.get_tick(token)

    def subscribe_live(self, tokens: list[str]):
        if self._feed:
            self._feed.ensure_subscribed(tokens)

    def get_candles(self, exchange: str, symbol: str, token: str,
                     interval: str, from_dt: str, to_dt: str) -> pd.DataFrame:
        resample_minutes = None
        if interval == "FIVE_MINUTE":
            resolution, resample_minutes = "1", 5
        elif interval == "ONE_DAY":
            resolution = "D"
        else:
            raise BrokerError(f"Alice Blue: unsupported interval {interval}")

        from_ts = int(IST.localize(dt.datetime.strptime(from_dt, "%Y-%m-%d %H:%M")).timestamp() * 1000)
        to_ts = int(IST.localize(dt.datetime.strptime(to_dt, "%Y-%m-%d %H:%M")).timestamp() * 1000)

        payload = {"token": str(token), "exchange": exchange, "from": str(from_ts),
                   "to": str(to_ts), "resolution": resolution}
        try:
            resp = httpx.post(HISTORICAL_URL, headers=self._headers(), json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise BrokerError(f"Alice Blue candle fetch failed for {symbol}: {e}") from e

        if str(data.get("stat", "")).lower() in ("not_ok", "not ok") or "result" not in data:
            raise BrokerError(f"Alice Blue candle fetch failed for {symbol}: {data.get('emsg', data)}")

        df = pd.DataFrame(data["result"])
        if df.empty:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.rename(columns={"time": "ts"})
        df["ts"] = pd.to_datetime(df["ts"])
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric)
        df = df.sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)

        if resample_minutes:
            df = df.set_index("ts").resample(f"{resample_minutes}min", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna(subset=["open"]).reset_index()

        return df[["ts", "open", "high", "low", "close", "volume"]]

    def place_order(self, exchange: str, symbol: str, token: str,
                     side: str, quantity: int, order_type: str = "MARKET",
                     price: float = 0) -> str:
        payload = [{
            "exchange": exchange,
            "instrumentId": str(int(float(token))),
            "transactionType": side,
            "quantity": int(quantity),
            "product": "INTRADAY",
            "orderComplexity": "REGULAR",
            "orderType": order_type,
            "validity": "DAY",
            "price": str(price or 0),
            "slLegPrice": "", "targetLegPrice": "", "slTriggerPrice": "0",
            "disclosedQuantity": "", "marketProtectionPercent": "", "deviceId": "",
            "trailingSlAmount": "", "apiOrderSource": "", "algoId": "",
            "orderTag": "scanner",
        }]
        try:
            resp = httpx.post(f"{REST_BASE}/open-api/od/v1/orders/placeorder",
                               headers=self._headers(), json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise BrokerError(f"Alice Blue order failed for {symbol}: {e}") from e

        if data.get("status") != "Ok":
            raise BrokerError(f"Alice Blue order rejected: {data.get('message', data)}")
        results = data.get("result", [])
        order_id = results[0].get("brokerOrderId") if results else None
        if not order_id:
            raise BrokerError(f"Alice Blue order rejected: {data}")
        return order_id

    def get_available_funds(self) -> float:
        try:
            resp = httpx.get(f"{REST_BASE}/open-api/od/v1/limits/", headers=self._headers(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise BrokerError(f"Alice Blue funds fetch failed: {e}") from e
        if data.get("status") != "Ok" or not data.get("result"):
            raise BrokerError(f"Alice Blue funds fetch failed: {data.get('message', data)}")
        return float(data["result"][0].get("tradingLimit", 0) or 0)
