import itertools
from .base import BrokerAdapter

_order_id_counter = itertools.count(1)


class PaperBroker(BrokerAdapter):
    """Sandbox broker: reads real live market data from a connected real
    broker, but never sends orders anywhere - it just books a simulated fill
    at the requested price/LTP and tracks a virtual cash balance in-process.
    """

    name = "paper"

    def __init__(self, data_source: BrokerAdapter, starting_capital: float = 1_000_000.0):
        self._data_source = data_source
        self.available_funds = starting_capital

    def connect(self) -> None:
        # No-op: broker_manager.connect_account() already connects the real
        # data source before wrapping it here. Calling connect() again would
        # open a second session/feed on the same underlying broker account.
        pass

    def get_ltp(self, exchange: str, symbol: str, token: str) -> float:
        return self._data_source.get_ltp(exchange, symbol, token)

    def get_candles(self, exchange: str, symbol: str, token: str,
                     interval: str, from_dt: str, to_dt: str):
        return self._data_source.get_candles(exchange, symbol, token, interval, from_dt, to_dt)

    def place_order(self, exchange: str, symbol: str, token: str,
                     side: str, quantity: int, order_type: str = "MARKET",
                     price: float = 0) -> str:
        fill_price = price or self.get_ltp(exchange, symbol, token)
        cost = fill_price * quantity
        if side == "BUY":
            self.available_funds -= cost
        else:
            self.available_funds += cost
        return f"PAPER-{next(_order_id_counter)}"

    def get_available_funds(self) -> float:
        return self.available_funds

    def get_live_quote(self, token: str) -> dict | None:
        get_live_quote = getattr(self._data_source, "get_live_quote", None)
        return get_live_quote(token) if get_live_quote else None

    def subscribe_live(self, tokens: list[str]) -> None:
        subscribe_live = getattr(self._data_source, "subscribe_live", None)
        if subscribe_live:
            subscribe_live(tokens)
