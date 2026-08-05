import logging
import threading
import time
import types

from SmartApi.smartWebSocketV2 import SmartWebSocketV2

log = logging.getLogger("angel_one_ws")


class AngelOneFeed:
    """Live tick feed over Angel One's SmartAPI WebSocket (QUOTE mode), so the
    dashboard can show real-time price/change updates without hammering the
    REST LTP endpoint (which is what triggered the rate-limit errors during
    a full poll-based refresh).

    QUOTE mode conveniently includes both last_traded_price and
    open_price_of_the_day in every tick, so change% can be computed purely
    from the stream - no need to separately cache the day's open.
    """

    NSE_CM = 1  # SmartWebSocketV2 exchangeType for NSE cash market (equities + indices)

    def __init__(self, auth_token: str, api_key: str, client_code: str, feed_token: str):
        self._ws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._subscribed_tokens: set[str] = set()
        self._last_tick: dict[str, dict] = {}  # token -> {ltp, open, ts}
        self._stop_requested = False
        self._last_close_ts = 0.0

        self._ws.on_open = self._on_open
        self._ws.on_data = self._on_data
        self._ws.on_error = self._on_error
        self._ws.on_close = self._on_close

        # smartapi-python 1.4.8's own SmartWebSocketV2._on_close(self, wsapp)
        # only accepts one argument, but the installed websocket-client
        # (1.8.0) calls on_close with (ws, close_status_code, close_msg) -
        # a version mismatch in the third-party library that crashes with
        # "takes 2 positional arguments but 4 were given" on every
        # disconnect. Patch it on this instance only (not the installed
        # package) to swallow the extra args and still call our on_close hook.
        def _patched_on_close(ws_self, wsapp, *_args, **_kwargs):
            ws_self.on_close(wsapp)
        self._ws._on_close = types.MethodType(_patched_on_close, self._ws)

    def start(self):
        self._stop_requested = False
        threading.Thread(target=self._ws.connect, daemon=True).start()
        if not self._connected.wait(timeout=10):
            raise RuntimeError("Timed out connecting to Angel One WebSocket")

    def stop(self):
        self._stop_requested = True
        try:
            self._ws.close_connection()
        except Exception:
            pass

    def _reconnect_after_delay(self):
        time.sleep(5)
        if self._stop_requested:
            return
        log.info("Reconnecting Angel One WebSocket after disconnect")
        try:
            self._ws.connect()
        except Exception as e:
            log.warning("Angel One WebSocket reconnect failed: %s", e)

    def _on_open(self, wsapp):
        log.info("Angel One WebSocket open")
        self._connected.set()
        with self._lock:
            tokens = list(self._subscribed_tokens)
        if tokens:
            self._ws.subscribe("scanner", 2, [{"exchangeType": self.NSE_CM, "tokens": tokens}])

    def _on_data(self, wsapp, data):
        token = data.get("token")
        if not token:
            log.warning("Angel One WS tick with no token: %s", data)
            return
        ltp = data.get("last_traded_price")
        open_price = data.get("open_price_of_the_day")
        if ltp is None:
            return
        with self._lock:
            entry = self._last_tick.setdefault(token, {})
            entry["ltp"] = ltp / 100
            if open_price:
                entry["open"] = open_price / 100
            entry["ts"] = time.time()
            n_ticks = len(self._last_tick)
        if n_ticks <= 3:
            log.info("First ticks arriving: token=%s ltp=%s open=%s", token, ltp, open_price)

    def _on_error(self, wsapp, error):
        log.warning("Angel One WebSocket error: %s", error)

    def _on_close(self, wsapp):
        self._connected.clear()
        if self._stop_requested:
            return
        # websocket-client can invoke on_close more than once for a single
        # disconnect (e.g. once from its error handler, once from normal
        # teardown) - seen in practice as two near-identical "Reconnecting"
        # log lines at the same millisecond, which then raced two connect()
        # calls against the same instance and caused a connect/disconnect
        # loop. Collapse any on_close calls within 2s of each other into one.
        now = time.time()
        with self._lock:
            if now - self._last_close_ts < 2:
                return
            self._last_close_ts = now
        threading.Thread(target=self._reconnect_after_delay, daemon=True).start()

    def ensure_subscribed(self, tokens: list[str]):
        with self._lock:
            new_tokens = [t for t in tokens if t and t not in self._subscribed_tokens]
            self._subscribed_tokens.update(new_tokens)
        log.info("Subscribing %d new tokens (connected=%s)", len(new_tokens), self._connected.is_set())
        if new_tokens and self._connected.is_set():
            self._ws.subscribe("scanner", 2, [{"exchangeType": self.NSE_CM, "tokens": new_tokens}])

    def get_tick(self, token: str) -> dict | None:
        with self._lock:
            return self._last_tick.get(token)
