import datetime as dt
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from . import config

engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    label = Column(String, nullable=False)
    broker = Column(String, nullable=False)  # "angel_one" | "alice_blue"
    mode = Column(String, nullable=False, default="sandbox")  # "sandbox" | "live"
    enabled = Column(Boolean, default=True)

    # Encrypted credential blobs - meaning depends on broker
    cred_client_id = Column(String, default="")
    cred_api_key = Column(String, default="")
    cred_secret = Column(String, default="")   # Angel: api secret (rarely needed) / Alice: app secret
    cred_password = Column(String, default="")  # login password / PIN
    cred_totp_secret = Column(String, default="")  # Angel One TOTP seed
    cred_session_token = Column(String, default="")  # Alice Blue: session token from OAuth callback

    status = Column(String, default="disconnected")  # disconnected | connected | error
    status_message = Column(String, default="")
    available_funds = Column(Float, nullable=True)

    # Per-account position sizing, so each connected account (which can have
    # very different capital) sizes its own trades independently.
    #  "pct"          - capital_per_trade_pct % of THIS account's available funds
    #  "fixed_amount" - a flat Rs amount per trade, regardless of funds
    sizing_mode = Column(String, default="pct")  # "pct" | "fixed_amount"
    capital_per_trade_pct = Column(Float, default=20.0)
    fixed_amount_per_trade = Column(Float, default=0.0)
    max_trades_per_day = Column(Integer, default=5)  # per-account, not a shared daily counter

    created_at = Column(DateTime, default=dt.datetime.utcnow)


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=dt.datetime.utcnow)
    symbol = Column(String)
    sector = Column(String)
    direction = Column(String)  # BUY | SELL
    trigger_low = Column(Float)
    trigger_high = Column(Float)
    status = Column(String, default="pending")  # pending | approved | rejected | executed | expired
    detail = Column(Text, default="")

    # Concrete prices for the trade, resolved when the signal fires rather
    # than left implicit in trigger_low/trigger_high. entry_price is the
    # close of the breakout candle; stop_loss_price and target_price follow
    # whichever mode is configured in StrategySettings (sl_mode/target_mode).
    entry_price = Column(Float, nullable=True)
    target_price = Column(Float, nullable=True)
    stop_loss_price = Column(Float, nullable=True)

    # Outcome tracking runs for EVERY signal, executed or not, so the
    # strategy can be reviewed on its own merits rather than only on the
    # trades that happened to be approved.
    outcome = Column(String, default="open")  # open | target_hit | sl_hit | squared_off
    outcome_price = Column(Float, nullable=True)
    outcome_ts = Column(DateTime, nullable=True)
    outcome_pnl_pct = Column(Float, nullable=True)  # notional %, independent of position size
    executed = Column(Boolean, default=False)       # did a real/paper order actually get placed


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=dt.datetime.utcnow)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    symbol = Column(String)
    side = Column(String)  # BUY | SELL
    quantity = Column(Integer)
    price = Column(Float)
    target_price = Column(Float, nullable=True)
    mode = Column(String)  # "paper" | "live" - whether this order was simulated or a real broker order
    status = Column(String, default="open")  # open | closed | rejected
    stop_loss = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_ts = Column(DateTime, nullable=True)
    pnl = Column(Float, default=0.0)
    broker_order_id = Column(String, default="")
    note = Column(Text, default="")

    account = relationship("Account")


class EngineState(Base):
    __tablename__ = "engine_state"

    id = Column(Integer, primary_key=True)
    run_mode = Column(String, default="auto")  # "manual" (approve each signal) | "auto" (auto-execute)
    active = Column(Boolean, default=True)  # fresh installs start ready to run - see main.py's startup resume logic
    # Single global switch: True = simulate fills locally (no order ever
    # reaches the broker), False = place real orders. Defaults to paper so a
    # fresh install can never accidentally trade real money before it's
    # explicitly flipped to live.
    paper_mode = Column(Boolean, default=True)
    trading_day = Column(String, default="")
    trades_today = Column(Integer, default=0)
    realized_pnl_today = Column(Float, default=0.0)
    target_hit = Column(Boolean, default=False)
    bias = Column(String, default="")  # bullish | bearish | mixed
    last_scan_ts = Column(DateTime, nullable=True)

    # Scan/indicator results, persisted as JSON so a server restart doesn't
    # lose the last scan (engine.py also keeps an in-memory copy for speed).
    watchlist_json = Column(Text, default="[]")
    scan_detail_json = Column(Text, default="{}")
    indicator_status_json = Column(Text, default="{}")
    all_stocks_json = Column(Text, default="[]")
    index_reads_json = Column(Text, default="{}")


class DailySummary(Base):
    """One row per trading day, snapshotted right before EngineState's
    daily counters reset (see strategy.engine.get_state) - trades_today and
    realized_pnl_today only ever showed *today*, with no way to look back
    at how the strategy did yesterday or last week."""
    __tablename__ = "daily_summary"

    id = Column(Integer, primary_key=True)
    trading_day = Column(String, unique=True)
    bias = Column(String, default="")
    trades_taken = Column(Integer, default=0)
    realized_pnl = Column(Float, default=0.0)
    target_hit = Column(Boolean, default=False)
    paper_mode = Column(Boolean, default=True)
    signals_fired = Column(Integer, default=0)
    signals_target_hit = Column(Integer, default=0)
    signals_sl_hit = Column(Integer, default=0)


class StrategySettings(Base):
    """User-configurable fund/risk/scan parameters. Single-row table, same
    pattern as EngineState. Changing behavior requires an explicit edit via
    the Settings tab."""
    __tablename__ = "strategy_settings"

    id = Column(Integer, primary_key=True)
    # Capital-per-trade sizing and max-trades-per-day live on Account
    # instead (sizing_mode/capital_per_trade_pct/fixed_amount_per_trade/
    # max_trades_per_day), so accounts with different capital can size and
    # cap their own trades independently.
    daily_profit_target_pct = Column(Float, default=5.0)
    sector_move_threshold_pct = Column(Float, default=1.0)
    stock_move_threshold_pct = Column(Float, default=1.0)
    scan_time = Column(String, default="09:30")
    squareoff_time = Column(String, default="15:15")
    # Informational-only snapshots (see engine.run_premarket_snapshot /
    # run_postmarket_snapshot) - these refresh index/gainers/losers/sector
    # display data but never touch the trading watchlist, since acting on
    # noisy pre-open prices should be avoided, and the trading day is
    # already over by the time the post-market snapshot runs.
    premarket_scan_time = Column(String, default="09:05")
    postmarket_scan_time = Column(String, default="15:35")
    # Per-trade target price, two selectable modes:
    #  "r_multiple" - target = entry +/- (stop distance x target_r_multiple).
    #                 Risk-aware: a tighter stop gives a tighter target.
    #  "fixed_pct"  - target = entry +/- (entry x target_pct / 100).
    #                 A plain, constant % move regardless of stop distance -
    #                 this is what most users mean by "N% target" and is the
    #                 default, since daily_profit_target_pct (above) is a
    #                 totally separate, PORTFOLIO-wide circuit breaker, not a
    #                 per-trade number - the two are easy to conflate.
    target_mode = Column(String, default="fixed_pct")  # "r_multiple" | "fixed_pct"
    target_r_multiple = Column(Float, default=2.0)
    target_pct = Column(Float, default=5.0)

    # Stop loss price, three selectable modes:
    #  "candle"       - breakout candle's low (long) / high (short), moves
    #                   with each candle's own range.
    #  "fixed_pct"    - entry -/+ (entry x sl_pct / 100). Constant % move.
    #  "fixed_points" - entry -/+ sl_points (a flat Rs distance, not a %).
    # NOTE: when target_mode="r_multiple", the target is still derived from
    # WHATEVER stop this produces (stop distance x R) - changing sl_mode
    # changes the target too in that mode, by design.
    sl_mode = Column(String, default="candle")  # "candle" | "fixed_pct" | "fixed_points"
    sl_pct = Column(Float, default=1.0)
    sl_points = Column(Float, default=1.0)

    # Confirmation stack toggles - a user who doesn't trust/want a given
    # indicator can turn it off; a signal only needs the CHECKED directional
    # indicators (ema/vwap/supertrend/rsi) to agree. adx/volume are gates
    # (trend-strength / above-average-volume), not directional reads, so
    # disabling them just removes that gate rather than needing "agreement".
    use_ema = Column(Boolean, default=True)
    use_vwap = Column(Boolean, default=True)
    use_supertrend = Column(Boolean, default=True)
    use_rsi = Column(Boolean, default=True)
    use_adx = Column(Boolean, default=True)
    use_volume = Column(Boolean, default=True)

    # Outgoing webhook - POSTs a JSON payload to this URL whenever a signal
    # fires or an order executes, so the same strategy can be piped into
    # any other algo platform / bot that accepts a generic webhook, without
    # that platform needing to know anything about Angel One or Alice Blue.
    webhook_url = Column(String, default="")
    webhook_enabled = Column(Boolean, default=False)


def _migrate_add_missing_columns():
    """SQLite's create_all only adds new tables, not new columns on existing
    ones - add any columns introduced after the initial schema by hand."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    def add_columns(table, additions):
        if table not in table_names:
            return
        existing = {col["name"] for col in inspector.get_columns(table)}
        with engine.begin() as conn:
            for col, coltype in additions.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))

    add_columns("accounts", {
        "available_funds": "FLOAT",
        "cred_session_token": "VARCHAR DEFAULT ''",
        "sizing_mode": "VARCHAR DEFAULT 'pct'",
        "capital_per_trade_pct": "FLOAT DEFAULT 20.0",
        "fixed_amount_per_trade": "FLOAT DEFAULT 0.0",
        "max_trades_per_day": "INTEGER DEFAULT 5",
    })
    add_columns("engine_state", {
        "watchlist_json": "TEXT DEFAULT '[]'",
        "scan_detail_json": "TEXT DEFAULT '{}'",
        "indicator_status_json": "TEXT DEFAULT '{}'",
        "all_stocks_json": "TEXT DEFAULT '[]'",
        "index_reads_json": "TEXT DEFAULT '{}'",
    })
    add_columns("strategy_settings", {
        "premarket_scan_time": "VARCHAR DEFAULT '09:05'",
        "postmarket_scan_time": "VARCHAR DEFAULT '15:35'",
        "target_r_multiple": "FLOAT DEFAULT 2.0",
        "target_mode": "VARCHAR DEFAULT 'fixed_pct'",
        "target_pct": "FLOAT DEFAULT 5.0",
        "use_ema": "BOOLEAN DEFAULT 1",
        "use_vwap": "BOOLEAN DEFAULT 1",
        "use_supertrend": "BOOLEAN DEFAULT 1",
        "use_rsi": "BOOLEAN DEFAULT 1",
        "use_adx": "BOOLEAN DEFAULT 1",
        "use_volume": "BOOLEAN DEFAULT 1",
        "webhook_url": "VARCHAR DEFAULT ''",
        "webhook_enabled": "BOOLEAN DEFAULT 0",
        "sl_mode": "VARCHAR DEFAULT 'candle'",
        "sl_pct": "FLOAT DEFAULT 1.0",
        "sl_points": "FLOAT DEFAULT 1.0",
    })
    add_columns("engine_state", {"paper_mode": "BOOLEAN DEFAULT 1"})
    add_columns("signals", {
        "entry_price": "FLOAT",
        "target_price": "FLOAT",
        "stop_loss_price": "FLOAT",
        "outcome": "VARCHAR DEFAULT 'open'",
        "outcome_price": "FLOAT",
        "outcome_ts": "DATETIME",
        "outcome_pnl_pct": "FLOAT",
        "executed": "BOOLEAN DEFAULT 0",
    })
    add_columns("orders", {"target_price": "FLOAT"})


def init_db():
    Base.metadata.create_all(engine)
    _migrate_add_missing_columns()
    with SessionLocal() as db:
        if not db.query(EngineState).first():
            db.add(EngineState())
            db.commit()
        if not db.query(StrategySettings).first():
            db.add(StrategySettings())
            db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
