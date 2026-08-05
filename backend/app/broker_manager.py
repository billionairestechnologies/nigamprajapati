from . import security
from .database import Account
from .brokers import AngelOneBroker, AliceBlueBroker, PaperBroker, BrokerAdapter

# account_id -> live BrokerAdapter instance (angel_one / alice_blue connectors,
# or that same connector wrapped in a PaperBroker if the account is sandbox)
_connections: dict[int, BrokerAdapter] = {}


def _build_real_connector(account: Account) -> BrokerAdapter:
    if account.broker == "angel_one":
        return AngelOneBroker(
            client_id=security.decrypt(account.cred_client_id),
            password=security.decrypt(account.cred_password),
            api_key=security.decrypt(account.cred_api_key),
            totp_secret=security.decrypt(account.cred_totp_secret),
        )
    if account.broker == "alice_blue":
        return AliceBlueBroker(
            client_id=security.decrypt(account.cred_client_id),
            session_token=security.decrypt(account.cred_session_token),
        )
    raise ValueError(f"Unknown broker: {account.broker}")


def connect_account(account: Account) -> BrokerAdapter:
    # Always a real broker session - it's the market-data source regardless
    # of paper/live. Whether an order actually reaches the broker is decided
    # at execution time (see strategy.engine.execute_signal) by the global
    # paper_mode flag OR this account's own mode=="sandbox", not by how we
    # connect here.
    adapter = _build_real_connector(account)
    adapter.connect()
    _connections[account.id] = adapter
    return adapter


def disconnect_account(account_id: int) -> None:
    adapter = _connections.pop(account_id, None)
    if adapter is not None:
        stop_feed = getattr(adapter, "feed", None)
        if stop_feed is not None:
            stop_feed.stop()


def get_connection(account_id: int) -> BrokerAdapter | None:
    return _connections.get(account_id)


def is_connected(account_id: int) -> bool:
    return account_id in _connections
