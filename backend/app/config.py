import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-me")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8787"))

DB_PATH = DATA_DIR / "scanner.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

if not APP_SECRET_KEY:
    raise RuntimeError(
        "APP_SECRET_KEY is not set. Copy backend/.env.example to backend/.env and "
        "generate a key with: python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\""
    )

if DASHBOARD_PASSWORD == "change-me":
    # run.bat's first-run message already tells the client to change this in
    # backend\.env, but until now nothing actually enforced it - the app
    # would just start with a well-known default password guarding broker
    # credentials and live/paper trading controls. Since this dashboard is
    # reachable over AnyDesk (and HOST=0.0.0.0 is an explicitly supported
    # option for reaching it from other machines), refuse to start rather
    # than silently run on a default credential.
    raise RuntimeError(
        "DASHBOARD_PASSWORD is still the default 'change-me'. Open backend\\.env and "
        "set DASHBOARD_USERNAME / DASHBOARD_PASSWORD to something private, then run "
        "run.bat again."
    )

# Fund/risk/scan parameters (capital per trade, max trades/day, profit
# target, sector/stock move thresholds, scan/square-off times) are
# user-configurable at runtime via the Settings tab and Accounts tab - see
# database.StrategySettings, database.Account, and strategy.engine.get_settings().
# The index/sector/stock universe is defined in strategy/universe.json.
