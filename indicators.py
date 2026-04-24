"""
Technical Indicators: VWAP, EMA, ATR, Z-Score
All calculations use pandas for speed and accuracy.
"""

import numpy as np
import pandas as pd

# ─── VWAP ─────────────────────────────────────────────────────────────────────
def calculate_vwap(df: pd.DataFrame) -> float:
    """
    Volume Weighted Average Price.
    Formula: VWAP = Cumulative(Price × Volume) / Cumulative(Volume)
    df must have: high, low, close, volume columns
    """
    try:
        typical_price       = (df["high"] + df["low"] + df["close"]) / 3
        cum_vol             = df["volume"].cumsum()
        cum_tp_vol          = (typical_price * df["volume"]).cumsum()
        df["vwap"]          = cum_tp_vol / cum_vol
        return round(float(df["vwap"].iloc[-1]), 2)
    except Exception:
        return 0.0


# ─── EMA ──────────────────────────────────────────────────────────────────────
def calculate_ema(df: pd.DataFrame, period: int = 20) -> float:
    """
    Exponential Moving Average.
    Uses pandas ewm for accuracy.
    """
    try:
        ema = df["close"].ewm(span=period, adjust=False).mean()
        return round(float(ema.iloc[-1]), 2)
    except Exception:
        return 0.0


def calculate_ema_series(df: pd.DataFrame, period: int) -> pd.Series:
    """Return full EMA series for trend detection."""
    return df["close"].ewm(span=period, adjust=False).mean()


# ─── ATR (Average True Range) ─────────────────────────────────────────────────
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    ATR = Average of True Range over N periods.
    True Range = max(H-L, |H-Prev_C|, |L-Prev_C|)
    Used for dynamic stop-loss calculation.
    """
    try:
        high  = df["high"]
        low   = df["low"]
        close = df["close"]

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low  - close.shift(1)).abs()

        tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        return round(float(atr.iloc[-1]), 2)
    except Exception:
        return 0.0


# ─── RSI ──────────────────────────────────────────────────────────────────────
def calculate_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """
    Relative Strength Index.
    RSI > 70 = Overbought (don't buy)
    RSI < 30 = Oversold (good to buy on bullish day)
    """
    try:
        delta  = df["close"].diff()
        gain   = delta.where(delta > 0, 0.0)
        loss   = -delta.where(delta < 0, 0.0)
        avg_g  = gain.ewm(span=period, adjust=False).mean()
        avg_l  = loss.ewm(span=period, adjust=False).mean()
        rs     = avg_g / avg_l
        rsi    = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 2)
    except Exception:
        return 50.0


# ─── MACD ─────────────────────────────────────────────────────────────────────
def calculate_macd(df: pd.DataFrame) -> dict:
    """
    MACD = EMA(12) - EMA(26)
    Signal = EMA(9) of MACD
    Histogram = MACD - Signal
    """
    try:
        ema12     = df["close"].ewm(span=12, adjust=False).mean()
        ema26     = df["close"].ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal    = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal
        return {
            "macd":      round(float(macd_line.iloc[-1]), 4),
            "signal":    round(float(signal.iloc[-1]), 4),
            "histogram": round(float(histogram.iloc[-1]), 4),
        }
    except Exception:
        return {"macd": 0, "signal": 0, "histogram": 0}


# ─── Bollinger Bands ──────────────────────────────────────────────────────────
def calculate_bollinger(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> dict:
    """
    Upper Band = SMA + 2×StdDev
    Lower Band = SMA - 2×StdDev
    Price near Lower Band = Oversold = potential buy on bullish day
    """
    try:
        sma   = df["close"].rolling(window=period).mean()
        std_v = df["close"].rolling(window=period).std()
        upper = sma + (std_v * std)
        lower = sma - (std_v * std)
        return {
            "upper":  round(float(upper.iloc[-1]), 2),
            "middle": round(float(sma.iloc[-1]), 2),
            "lower":  round(float(lower.iloc[-1]), 2),
            "width":  round(float((upper - lower).iloc[-1] / sma.iloc[-1] * 100), 2),
        }
    except Exception:
        return {"upper": 0, "middle": 0, "lower": 0, "width": 0}


# ─── Z-Score (for Arbitrage) ──────────────────────────────────────────────────
def calculate_zscore(spread_series: pd.Series, window: int = 20) -> float:
    """
    Z-Score = (Current - Mean) / StdDev
    Z > 2.5 = Extreme dislocation = Arb entry
    Z < 0.5 = Spread normalized = Arb exit
    """
    try:
        mean  = spread_series.rolling(window=window).mean()
        std   = spread_series.rolling(window=window).std()
        z     = (spread_series - mean) / std
        return round(float(z.iloc[-1]), 3)
    except Exception:
        return 0.0


# ─── Market Bias ──────────────────────────────────────────────────────────────
def get_market_bias(current_price: float, vwap: float,
                    ema20: float, ema200: float) -> str:
    """
    Determine overall market direction.

    STRONG_BULL : Price > VWAP > EMA20 > EMA200
    BULL        : Price > VWAP and Price > EMA20
    BEAR        : Price < VWAP and Price < EMA20
    STRONG_BEAR : Price < VWAP < EMA20 < EMA200
    NEUTRAL     : Oscillating — bot stays flat
    """
    if current_price <= 0 or vwap <= 0:
        return "NEUTRAL"

    above_vwap  = current_price > vwap
    above_ema20 = current_price > ema20
    above_ema200= current_price > ema200

    if above_vwap and above_ema20 and above_ema200:
        if vwap > ema20 > ema200:
            return "STRONG_BULL"
        return "BULL"
    elif not above_vwap and not above_ema20:
        if vwap < ema20:
            return "STRONG_BEAR"
        return "BEAR"
    else:
        return "NEUTRAL"


# ─── Dynamic Quantity Sizing ──────────────────────────────────────────────────
def calculate_position_size(capital: float, risk_pct: float,
                            entry_price: float, sl_pct: float,
                            min_qty: int = 1) -> int:
    """
    Risk-Based Position Sizing.
    Formula: Qty = (Capital × Risk%) / (Entry × SL%)

    Example:
        Capital = 200000, Risk = 0.5%
        Entry   = 3000,   SL   = 0.35%
        Qty = (200000 × 0.005) / (3000 × 0.0035) = 1000/10.5 = 95
    """
    try:
        risk_amount = capital * (risk_pct / 100)
        sl_amount   = entry_price * (sl_pct / 100)
        if sl_amount <= 0:
            return min_qty
        qty = int(risk_amount / sl_amount)
        return max(qty, min_qty)
    except Exception:
        return min_qty


# ─── Trailing Stop ────────────────────────────────────────────────────────────
def calculate_trailing_sl(entry: float, current: float,
                          atr: float, side: str = "BUY") -> float:
    """
    ATR-Based Trailing Stop Loss.

    Levels:
      Profit >= 0.8% → move SL to breakeven + 0.05%
      Profit >= 1.2% → move SL to +0.8%
      Profit >= 1.8% → move SL to +1.2%
    """
    try:
        pnl_pct = (current - entry) / entry * 100 if side == "BUY" else (entry - current) / entry * 100

        if pnl_pct >= 1.8:
            return round(entry * 1.012, 2) if side == "BUY" else round(entry * 0.988, 2)
        elif pnl_pct >= 1.2:
            return round(entry * 1.008, 2) if side == "BUY" else round(entry * 0.992, 2)
        elif pnl_pct >= 0.8:
            return round(entry * 1.0005, 2) if side == "BUY" else round(entry * 0.9995, 2)
        else:
            # Default fixed SL
            return round(entry * 0.9965, 2) if side == "BUY" else round(entry * 1.0035, 2)
    except Exception:
        return entry * 0.9965