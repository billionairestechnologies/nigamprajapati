from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Quote:
    symbol: str
    ltp: float
    change_pct: float = 0.0


class BrokerError(Exception):
    pass


class BrokerAdapter(ABC):
    """Common interface every broker connector must implement.

    A connector wraps one already-authenticated broker session. Nothing here
    decides sandbox vs live - that's handled one layer up (see paper.py and
    the strategy engine), so the same real connector can be used to source
    live market data even for accounts running in sandbox mode.
    """

    name: str

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def get_ltp(self, exchange: str, symbol: str, token: str) -> float:
        ...

    @abstractmethod
    def get_candles(self, exchange: str, symbol: str, token: str,
                     interval: str, from_dt: str, to_dt: str):
        """Returns a pandas DataFrame with columns: ts, open, high, low, close, volume"""
        ...

    @abstractmethod
    def place_order(self, exchange: str, symbol: str, token: str,
                     side: str, quantity: int, order_type: str = "MARKET",
                     price: float = 0) -> str:
        """Returns broker order id."""
        ...

    @abstractmethod
    def get_available_funds(self) -> float:
        ...
