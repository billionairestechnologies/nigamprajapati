import secrets
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from . import config

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days - after that, log in again

# Reuses the same Fernet key that already encrypts broker credentials
# (backend/.env's APP_SECRET_KEY) to sign/encrypt the session cookie, rather
# than adding a whole new dependency (e.g. itsdangerous) just for this.
_fernet = Fernet(config.APP_SECRET_KEY.encode())


def check_credentials(username: str, password: str) -> bool:
    user_ok = secrets.compare_digest(username, config.DASHBOARD_USERNAME)
    pass_ok = secrets.compare_digest(password, config.DASHBOARD_PASSWORD)
    return user_ok and pass_ok


def create_session_token() -> str:
    return _fernet.encrypt(b"authenticated").decode()


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        return _fernet.decrypt(token.encode(), ttl=SESSION_MAX_AGE) == b"authenticated"
    except Exception:
        return False


def require_login(request: Request):
    """Dependency for API routes - a plain 401 is fine here since callers
    are our own dashboard JS, not a browser page navigation."""
    if not session_valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="Not authenticated")
