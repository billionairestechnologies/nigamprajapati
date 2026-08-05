"""EMA, VWAP, Supertrend, RSI, ADX, and Volume confirmation stack, computed
on a 5-minute candle DataFrame with columns: ts, open, high, low, close, volume.
"""
import warnings
import numpy as np
import pandas as pd

# pandas 2.2's copy-on-write preview raises a FutureWarning on plain
# `df["col"] = value` column assignment even on an explicitly-copied
# DataFrame (see evaluate_confirmation_stack). Values are correct either
# way; this just keeps the confirmation pass (which fires every 5 minutes
# all trading day) from flooding the log file.
warnings.filterwarnings("ignore", message=".*ChainedAssignmentError.*", category=FutureWarning)


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_vol_price = (typical * df["volume"]).cumsum()
    return cum_vol_price / cum_vol.replace(0, np.nan)


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / length, adjust=False).mean()


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = _true_range(df)
    atr_ = tr.ewm(alpha=1 / length, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / atr_.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.ewm(alpha=1 / length, adjust=False).mean()


def supertrend(df: pd.DataFrame, length: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    atr_ = atr(df, length)
    hl2 = (df["high"] + df["low"]) / 2
    upperband = hl2 + multiplier * atr_
    lowerband = hl2 - multiplier * atr_

    final_upper = upperband.copy()
    final_lower = lowerband.copy()
    trend = pd.Series(index=df.index, dtype="object")

    for i in range(len(df)):
        if i == 0:
            trend.iloc[i] = "up"
            continue
        if df["close"].iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = max(upperband.iloc[i], final_upper.iloc[i - 1]) \
                if df["close"].iloc[i - 1] > final_upper.iloc[i - 1] else upperband.iloc[i]
        final_upper.iloc[i] = upperband.iloc[i] if (
            upperband.iloc[i] < final_upper.iloc[i - 1] or df["close"].iloc[i - 1] > final_upper.iloc[i - 1]
        ) else final_upper.iloc[i - 1]
        final_lower.iloc[i] = lowerband.iloc[i] if (
            lowerband.iloc[i] > final_lower.iloc[i - 1] or df["close"].iloc[i - 1] < final_lower.iloc[i - 1]
        ) else final_lower.iloc[i - 1]

        if df["close"].iloc[i] > final_upper.iloc[i - 1]:
            trend.iloc[i] = "up"
        elif df["close"].iloc[i] < final_lower.iloc[i - 1]:
            trend.iloc[i] = "down"
        else:
            trend.iloc[i] = trend.iloc[i - 1]

    line = np.where(trend == "up", final_lower, final_upper)
    return pd.DataFrame({"supertrend": line, "trend": trend}, index=df.index)


_DEFAULT_ENABLED = {"ema": True, "vwap": True, "supertrend": True, "rsi": True, "adx": True, "volume": True}


def evaluate_confirmation_stack(df: pd.DataFrame, enabled: dict | None = None) -> dict:
    """Runs all five signals + volume on the latest closed 5-min candle and
    returns per-indicator bullish/bearish reads plus the overall alignment.
    Mixed reads mean the caller should skip the entry.

    `enabled` (StrategySettings.use_ema etc.) lets a user drop indicators
    they don't trust out of the alignment decision - reads are still
    computed and returned for every indicator regardless (so the dashboard
    can display them), only the ALIGNED verdict changes:
      - ema/vwap/supertrend/rsi are directional: only the enabled ones need
        to agree for a BUY/SELL. At least one must stay enabled by caller
        contract (validated in Settings) - if somehow all four are off,
        alignment can never fire (nothing to agree on).
      - adx/volume are gates, not directional reads: disabling one just
        removes that gate instead of requiring "agreement".
    """
    enabled = enabled or _DEFAULT_ENABLED
    if len(df) < 30:
        return {"aligned": None, "reason": "not enough candles for a stable read"}

    df = df.copy()
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["vwap"] = vwap(df)
    df["rsi"] = rsi(df["close"])
    df["adx"] = adx(df)
    st = supertrend(df)
    df["supertrend"] = st["supertrend"]
    df["st_trend"] = st["trend"]
    df["vol_avg20"] = df["volume"].rolling(20).mean()

    last = df.iloc[-1]

    reads = {
        "ema": "bull" if last["ema20"] > last["ema50"] else "bear",
        "vwap": "bull" if last["close"] > last["vwap"] else "bear",
        "supertrend": "bull" if last["st_trend"] == "up" else "bear",
        "rsi": "bull" if last["rsi"] > 55 else ("bear" if last["rsi"] < 45 else "neutral"),
        "adx": "trending" if last["adx"] > 20 else "weak",
        "volume": "confirm" if last["volume"] > last["vol_avg20"] else "weak",
    }

    directional_names = [n for n in ("ema", "vwap", "supertrend", "rsi") if enabled.get(n, True)]
    directional = [reads[n] for n in directional_names]
    all_bull = bool(directional) and all(v == "bull" for v in directional)
    all_bear = bool(directional) and all(v == "bear" for v in directional)
    trend_ok = (not enabled.get("adx", True)) or reads["adx"] == "trending"
    volume_ok = (not enabled.get("volume", True)) or reads["volume"] == "confirm"

    if all_bull and trend_ok and volume_ok:
        aligned = "BUY"
    elif all_bear and trend_ok and volume_ok:
        aligned = "SELL"
    else:
        aligned = None

    return {
        "aligned": aligned,
        "reads": reads,
        "trigger_candle": {
            "low": float(last["low"]),
            "high": float(last["high"]),
            "close": float(last["close"]),
            "ts": str(last["ts"]),
        },
    }
