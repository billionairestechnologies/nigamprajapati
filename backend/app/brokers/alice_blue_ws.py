import hashlib
import json
import logging
import threading
import time

import httpx
import websocket

log = logging.getLogger("alice_blue_ws")

REST_BASE = "https://a3.aliceblueonline.com/"
PRIMARY_WS_URL = "wss://ws1.aliceblueonline.com/NorenWS/"
ALTERNATE_WS_URL = "wss://ws2.aliceblueonline.com/NorenWS/"


class AliceBlueFeed:
    """Minimal WebSocket tick feed for Alice Blue's ANT / V2 API.

    Alice Blue's current API has no simple "give me the LTP right now" REST
    call - live prices only come over this WebSocket. We keep one connection
    per broker account, subscribe lazily to whatever symbols get asked for,
    and cache the last tick per token so get_ltp() can return synchronously.
    """

    def __init__(self, user_id: str, session_token: str):
        self.user_id = user_id
        self.session_token = session_token
        self._ws = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._subscribed: set[str] = set()
        self._last_quote: dict[str, dict] = {}
        self._stop = threading.Event()
        self._last_close_ts = 0.0
        enc1 = hashlib.sha256(session_token.encode()).hexdigest()
        self._enc_token = hashlib.sha256(enc1.encode()).hexdigest()

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.session_token}", "Content-Type": "application/json"}

    def _create_ws_session(self) -> bool:
        try:
            with httpx.Client(timeout=10) as client:
                client.post(REST_BASE + "open-api/od/v1/profile/invalidateWsSess",
                            json={"source": "API", "userId": self.user_id}, headers=self._auth_headers())
                resp = client.post(REST_BASE + "open-api/od/v1/profile/createWsSess",
                                    json={"source": "API", "userId": self.user_id}, headers=self._auth_headers())
                return resp.json().get("status") == "Ok"
        except Exception as e:
            log.error("Alice Blue WS session setup failed: %s", e)
            return False

    def start(self):
        if not self._create_ws_session():
            raise RuntimeError("Could not create Alice Blue WebSocket session")
        threading.Thread(target=self._run_forever, daemon=True).start()
        if not self._connected.wait(timeout=10):
            raise RuntimeError("Timed out connecting to Alice Blue WebSocket")

    def stop(self):
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _run_forever(self):
        # Keep reconnecting (alternating primary/alternate) for the life of
        # this feed, not just once - the previous version returned after a
        # single pass through both URLs, so any disconnect (network blip,
        # broker-side restart) permanently killed live pricing for the rest
        # of the session with no visible error, silently going stale.
        urls = (PRIMARY_WS_URL, ALTERNATE_WS_URL)
        i = 0
        while not self._stop.is_set():
            url = urls[i % len(urls)]
            i += 1
            self._ws = websocket.WebSocketApp(
                url, on_open=self._on_open, on_message=self._on_message,
                on_error=self._on_error, on_close=self._on_close,
            )
            self._ws.run_forever()
            if self._stop.is_set():
                return
            log.warning("Alice Blue WebSocket disconnected, reconnecting in 5s")
            time.sleep(5)

    def _on_open(self, ws):
        ws.send(json.dumps({
            "susertoken": self._enc_token, "t": "c",
            "actid": f"{self.user_id}_API", "uid": f"{self.user_id}_API", "source": "API",
        }))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if data.get("s") == "OK" or (data.get("t") == "cf" and data.get("k") == "OK"):
            self._connected.set()
            with self._lock:
                subs = list(self._subscribed)
            if subs:
                ws.send(json.dumps({"t": "t", "k": "#".join(subs)}))
            return
        if data.get("t") in ("tk", "tf"):
            key = f"{data.get('e', '')}:{data.get('tk', '')}"
            with self._lock:
                existing = self._last_quote.get(key, {})
                if "lp" in data:
                    existing["ltp"] = float(data["lp"])
                if "o" in data:
                    existing["open"] = float(data["o"])
                if "h" in data:
                    existing["high"] = float(data["h"])
                if "l" in data:
                    existing["low"] = float(data["l"])
                if "c" in data:
                    existing["close"] = float(data["c"])
                if "v" in data:
                    existing["volume"] = int(data["v"])
                self._last_quote[key] = existing

    def _on_error(self, ws, error):
        log.warning("Alice Blue WebSocket error: %s", error)

    def _on_close(self, ws, status, msg):
        self._connected.clear()

    def subscribe_and_wait(self, exchange: str, token: str, timeout: float = 5.0) -> dict | None:
        key = f"{exchange}:{token}"
        with self._lock:
            already = key in self._subscribed
            self._subscribed.add(key)
        if not already and self._ws and self._connected.is_set():
            self._ws.send(json.dumps({"t": "t", "k": key}))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                quote = self._last_quote.get(key)
            if quote and "ltp" in quote:
                return quote
            time.sleep(0.2)
        with self._lock:
            return self._last_quote.get(key)

    def ensure_subscribed(self, tokens: list[str], exchange: str = "NSE"):
        """Bulk, non-blocking subscribe - same shape as Angel One's
        ensure_subscribed(), for parity: used by resubscribe_live_feed() to
        re-arm the whole watchlist's live feed after a restart/reconnect
        without waiting on any single tick."""
        keys = [f"{exchange}:{t}" for t in tokens if t]
        with self._lock:
            new_keys = [k for k in keys if k not in self._subscribed]
            self._subscribed.update(new_keys)
        if new_keys and self._ws and self._connected.is_set():
            self._ws.send(json.dumps({"t": "t", "k": "#".join(new_keys)}))

    def get_tick(self, token: str, exchange: str = "NSE") -> dict | None:
        with self._lock:
            return self._last_quote.get(f"{exchange}:{token}")
