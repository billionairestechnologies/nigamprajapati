from cryptography.fernet import Fernet
from . import config

_fernet = Fernet(config.APP_SECRET_KEY.encode())


def encrypt(value: str) -> str:
    if value is None:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    return _fernet.decrypt(token.encode()).decode()
