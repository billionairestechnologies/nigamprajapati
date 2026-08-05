import datetime as dt
from pydantic import BaseModel, field_validator

CREDENTIAL_FIELDS = ("client_id", "api_key", "secret", "password", "totp_secret")


def _reject_non_ascii(field_name: str, value: str) -> str:
    value = value.strip()
    if not value.isascii():
        raise ValueError(
            f"{field_name} contains a character that isn't plain text (likely a smart "
            "quote, invisible space, or dash from a copy-paste). Please clear the field "
            "and type the value manually instead of pasting it."
        )
    return value


class AccountCreate(BaseModel):
    label: str
    broker: str  # "angel_one" | "alice_blue"
    mode: str = "sandbox"  # "sandbox" | "live"
    client_id: str
    api_key: str
    secret: str = ""
    password: str = ""
    totp_secret: str = ""
    sizing_mode: str = "pct"  # "pct" | "fixed_amount"
    capital_per_trade_pct: float = 20.0
    fixed_amount_per_trade: float = 0.0
    max_trades_per_day: int = 5

    @field_validator(*CREDENTIAL_FIELDS)
    @classmethod
    def validate_credential(cls, value: str, info) -> str:
        return _reject_non_ascii(info.field_name, value)


class AccountUpdate(BaseModel):
    label: str | None = None
    mode: str | None = None
    enabled: bool | None = None
    sizing_mode: str | None = None
    capital_per_trade_pct: float | None = None
    fixed_amount_per_trade: float | None = None
    max_trades_per_day: int | None = None


class AccountOut(BaseModel):
    id: int
    label: str
    broker: str
    mode: str
    enabled: bool
    status: str
    status_message: str
    available_funds: float | None = None
    sizing_mode: str = "pct"
    capital_per_trade_pct: float = 20.0
    fixed_amount_per_trade: float = 0.0
    max_trades_per_day: int = 5
    estimated_capital_per_trade: float | None = None
    trades_today: int = 0

    class Config:
        from_attributes = True


class EngineModeUpdate(BaseModel):
    run_mode: str  # "manual" | "auto"


class PaperModeUpdate(BaseModel):
    paper_mode: bool


class SettingsOut(BaseModel):
    daily_profit_target_pct: float
    sector_move_threshold_pct: float
    stock_move_threshold_pct: float
    scan_time: str
    squareoff_time: str
    premarket_scan_time: str
    postmarket_scan_time: str
    target_mode: str = "fixed_pct"
    target_r_multiple: float = 2.0
    target_pct: float = 5.0
    sl_mode: str = "candle"
    sl_pct: float = 1.0
    sl_points: float = 1.0
    use_ema: bool = True
    use_vwap: bool = True
    use_supertrend: bool = True
    use_rsi: bool = True
    use_adx: bool = True
    use_volume: bool = True
    webhook_url: str = ""
    webhook_enabled: bool = False

    class Config:
        from_attributes = True


class SettingsUpdate(BaseModel):
    daily_profit_target_pct: float | None = None
    sector_move_threshold_pct: float | None = None
    stock_move_threshold_pct: float | None = None
    scan_time: str | None = None
    squareoff_time: str | None = None
    premarket_scan_time: str | None = None
    postmarket_scan_time: str | None = None
    target_mode: str | None = None
    target_r_multiple: float | None = None
    target_pct: float | None = None
    sl_mode: str | None = None
    sl_pct: float | None = None
    sl_points: float | None = None
    use_ema: bool | None = None
    use_vwap: bool | None = None
    use_supertrend: bool | None = None
    use_rsi: bool | None = None
    use_adx: bool | None = None
    use_volume: bool | None = None
    webhook_url: str | None = None
    webhook_enabled: bool | None = None


class WebhookTestRequest(BaseModel):
    url: str | None = None  # if omitted, uses the saved webhook_url


class SignalOut(BaseModel):
    id: int
    symbol: str
    sector: str
    direction: str
    status: str
    trigger_low: float
    trigger_high: float
    entry_price: float | None = None
    target_price: float | None = None
    stop_loss_price: float | None = None
    outcome: str = "open"
    outcome_price: float | None = None
    outcome_pnl_pct: float | None = None
    executed: bool = False

    class Config:
        from_attributes = True


class OrderOut(BaseModel):
    id: int
    ts: dt.datetime
    account_id: int
    symbol: str
    side: str
    quantity: int
    price: float
    mode: str
    status: str
    stop_loss: float | None
    target_price: float | None = None
    exit_price: float | None
    exit_ts: dt.datetime | None = None
    pnl: float
    note: str = ""

    class Config:
        from_attributes = True
