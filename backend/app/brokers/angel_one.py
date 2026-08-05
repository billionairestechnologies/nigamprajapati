import logging
import pandas as pd
import pyotp
from .base import BrokerAdapter, BrokerError
from .angel_one_ws import AngelOneFeed

log = logging.getLogger("angel_one")


class AngelOneBroker(BrokerAdapter):
    """Wraps angel-one/smartapi-python.

    Credentials required: client_id (Angel client code), password (login PIN),
    api_key (SmartAPI app key), totp_secret (the base32 seed shown once when
    you enable TOTP on the Angel One SmartAPI portal - NOT a 6 digit code).
    """

    name = "angel_one"

    def __init__(self, client_id: str, password: str, api_key: str, totp_secret: str):
        self.client_id = client_id
        self.password = password
        self.api_key = api_key
        self.totp_secret = totp_secret
        self._client = None
        self._feed_token = None
        self.feed: AngelOneFeed | None = None

    def connect(self) -> None:
        try:
            from SmartApi import SmartConnect
        except ImportError as e:
            raise BrokerError(
                "smartapi-python is not installed. Run: pip install smartapi-python"
            ) from e

        self._client = SmartConnect(api_key=self.api_key)
        totp = pyotp.TOTP(self.totp_secret).now()
        data = self._client.generateSession(self.client_id, self.password, totp)
        if not data or not data.get("status"):
            raise BrokerError(f"Angel One login failed: {data}")
        self._feed_token = self._client.getfeedToken()

        try:
            self.feed = AngelOneFeed(
                auth_token=data["data"]["jwtToken"],
                api_key=self.api_key,
                client_code=self.client_id,
                feed_token=data["data"]["feedToken"],
            )
            self.feed.start()
        except Exception as e:
            # Live streaming is a nice-to-have for the dashboard - trading and
            # scanning still work over REST (get_ltp falls back below) even
            # if the feed can't come up, so don't fail the whole connect.
            log.warning("Angel One connected but live feed failed to start: %s", e)
            self.feed = None

    def get_ltp(self, exchange: str, symbol: str, token: str) -> float:
        if self.feed:
            tick = self.feed.get_tick(token)
            if tick and "ltp" in tick:
                return tick["ltp"]
        resp = self._client.ltpData(exchange, symbol, token)
        if not resp or not resp.get("status"):
            raise BrokerError(f"Angel One LTP fetch failed for {symbol}: {resp}")
        return float(resp["data"]["ltp"])

    def get_live_quote(self, token: str) -> dict | None:
        """Instant cached tick (ltp + today's open) from the WebSocket feed,
        or None if that token hasn't been subscribed/ticked yet."""
        if not self.feed:
            return None
        return self.feed.get_tick(token)

    def subscribe_live(self, tokens: list[str]):
        if self.feed:
            self.feed.ensure_subscribed(tokens)

    def get_candles(self, exchange: str, symbol: str, token: str,
                     interval: str, from_dt: str, to_dt: str) -> pd.DataFrame:
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,  # e.g. "FIVE_MINUTE"
            "fromdate": from_dt,   # "YYYY-MM-DD HH:MM"
            "todate": to_dt,
        }
        resp = self._client.getCandleData(params)
        if not resp or not resp.get("status"):
            raise BrokerError(f"Angel One candle fetch failed for {symbol}: {resp}")
        rows = resp["data"]
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    def place_order(self, exchange: str, symbol: str, token: str,
                     side: str, quantity: int, order_type: str = "MARKET",
                     price: float = 0) -> str:
        order = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side,  # BUY | SELL
            "exchange": exchange,
            "ordertype": order_type,  # MARKET | LIMIT
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(price) if price else "0",
            "quantity": str(quantity),
        }
        resp = self._client.placeOrder(order)
        if not resp:
            raise BrokerError(f"Angel One order rejected: {resp}")
        order_id = resp if isinstance(resp, str) else resp.get("data", {}).get("orderid", "")
        if not order_id:
            raise BrokerError(f"Angel One order rejected: {resp}")
        return order_id

    def get_available_funds(self) -> float:
        resp = self._client.rmsLimit()
        if not resp or not resp.get("status"):
            raise BrokerError(f"Angel One funds fetch failed: {resp}")
        return float(resp["data"].get("net", 0) or 0)
