"""
Robust VWAP-EMA Hybrid Trading Engine
Dynamic watchlist — auto-selects best stocks meeting conditions
"""

import os
import time
import json
import logging
import threading
import pandas as pd
import numpy as np
from datetime import datetime, time as dtime
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not API_KEY or not API_SECRET:
    raise EnvironmentError(
        "\n[ERROR] API_KEY or API_SECRET not found in .env\n"
        "Create .env with:\n"
        "  API_KEY=your_key\n"
        "  API_SECRET=your_secret\n"
    )

log = logging.getLogger(__name__)

from indicators import (
    calculate_vwap, calculate_ema, calculate_ema_series,
    calculate_atr, calculate_rsi, calculate_macd,
    calculate_bollinger, calculate_zscore,
    get_market_bias, calculate_position_size, calculate_trailing_sl
)
from risk_manager import RiskManager

# ─── CONFIG ───────────────────────────────────────────────────────────────────
CONFIG = {
    "api_key":    API_KEY,
    "api_secret": API_SECRET,

    "strategy": {
        "mode":                "hybrid",
        "take_profit_pct":     1.5,
        "stop_loss_pct":       0.35,
        "max_trades_per_day":  6,
        "order_quantity":      1,
        "ema_fast":            9,
        "ema_slow":            20,
        "ema_trend":           200,
        "rsi_period":          14,
        "rsi_oversold":        45,
        "rsi_overbought":      70,
        "atr_period":          14,
        "candle_interval":     "5minute",
        "candle_lookback":     100,
        "vwap_entry_buffer":   0.0015,
        "use_dynamic_sizing":  True,
        "risk_per_trade_pct":  0.5,
        "trailing_sl":         True,
        "trade_cutoff_hour":   14,
        "trade_cutoff_minute": 0,
        # Dynamic watchlist settings
        "use_dynamic_watchlist": True,
        "dynamic_wl_size":       20,   # monitor top 20 NIFTY 50 stocks
        "min_price":             100,  # ignore penny stocks
        "max_price":             100000,
    },

    # ── Static fallback watchlist (used if dynamic fetch fails) ───────────────
    "watchlist": [
        {"symbol": "RELIANCE",   "exchange": "NSE", "qty": 1, "priority": 1},
        {"symbol": "HDFCBANK",   "exchange": "NSE", "qty": 1, "priority": 1},
        {"symbol": "ICICIBANK",  "exchange": "NSE", "qty": 1, "priority": 2},
        {"symbol": "INFY",       "exchange": "NSE", "qty": 1, "priority": 2},
        {"symbol": "TCS",        "exchange": "NSE", "qty": 1, "priority": 3},
        {"symbol": "SBIN",       "exchange": "NSE", "qty": 1, "priority": 3},
        {"symbol": "AXISBANK",   "exchange": "NSE", "qty": 1, "priority": 4},
        {"symbol": "KOTAKBANK",  "exchange": "NSE", "qty": 1, "priority": 4},
        {"symbol": "BAJFINANCE", "exchange": "NSE", "qty": 1, "priority": 5},
        {"symbol": "WIPRO",      "exchange": "NSE", "qty": 1, "priority": 5},
    ],

    "index": {
        "symbol":   "NIFTY 50",
        "exchange": "NSE",
    },

    "risk": {
        "max_daily_loss":     3000,
        "max_open_positions": 3,
        "capital_per_trade":  50000,
        "total_capital":      200000,
        "max_capital_deploy": 0.6,
    },

    "market_open":  dtime(9, 15),
    "market_close": dtime(15, 15),
    "token_file":   "access_token.json",
    "trade_log":    "trades.json",
}

# ─── NIFTY 50 UNIVERSE ────────────────────────────────────────────────────────
# All NIFTY 50 stocks — bot will pick best ones dynamically
NIFTY50_UNIVERSE = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BPCL", "BHARTIARTL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
    "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK", "LT",
    "LTIM", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
    "TECHM", "TITAN", "ULTRACEMCO", "WIPRO", "ZOMATO",
]

# ─── STATE ────────────────────────────────────────────────────────────────────
state = {
    "open_positions":    {},
    "daily_pnl":         0.0,
    "mtm_pnl":           0.0,
    "trade_count":       0,
    "halted":            False,
    "market_stop":       False,
    "ltp_cache":         {},
    "ohlcv_cache":       {},
    "indicator_cache":   {},
    "quote_cache":       {},   # raw Kite quote data
    "reference_prices":  {},
    "activity_log":      [],
    "profile":           {},
    "funds":             {},
    "strike_count":      {},
    "market_bias":       "NEUTRAL",
    "session_date":      "",
    "dynamic_watchlist": [],   # auto-updated watchlist
    "performance": {
        "total_trades":   0,
        "wins":           0,
        "losses":         0,
        "gross_pnl":      0.0,
        "brokerage_paid": 0.0,
        "best_trade":     0.0,
        "worst_trade":    0.0,
    }
}

# ─── KITE ─────────────────────────────────────────────────────────────────────
kite        = KiteConnect(api_key=CONFIG["api_key"])
_auth       = False
bot_running = False
_stop_event = threading.Event()
_state_lock = threading.Lock()
risk_mgr    = RiskManager(CONFIG, state)

# Token cache — avoid repeated instrument list fetches
_token_cache: dict = {}


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_authenticated() -> bool:
    return _auth


def is_market_open() -> bool:
    now = datetime.now().time()
    return CONFIG["market_open"] <= now <= CONFIG["market_close"]


def get_ltp_cached(symbol: str) -> float:
    return float(state["ltp_cache"].get(symbol, 0.0))


def add_log(msg: str, log_type: str = "info"):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "msg":  msg,
        "type": log_type,
    }
    with _state_lock:
        state["activity_log"].append(entry)
        if len(state["activity_log"]) > 300:
            state["activity_log"] = state["activity_log"][-300:]
    log.info(f"[{log_type.upper()}] {msg}")


def fmt_price(p: float) -> str:
    return f"₹{float(p):,.2f}"


def get_active_watchlist() -> list:
    """
    Return dynamic watchlist if available, else static fallback.
    """
    dyn = state.get("dynamic_watchlist", [])
    return dyn if dyn else CONFIG["watchlist"]


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════════════

def try_load_token():
    global _auth
    try:
        with open(CONFIG["token_file"]) as f:
            data = json.load(f)
        today = datetime.today().strftime("%Y-%m-%d")
        if data.get("date") == today:
            kite.set_access_token(data["token"])
            _auth = True
            _fetch_profile()
            add_log("Session restored from saved token", "info")
        else:
            log.info("[AUTH] Stale token.")
    except FileNotFoundError:
        log.info("[AUTH] No token file.")
    except Exception as e:
        log.error(f"[AUTH] {e}")


def authenticate(request_token: str):
    global _auth
    data  = kite.generate_session(request_token,
                                  api_secret=CONFIG["api_secret"])
    token = data["access_token"]
    kite.set_access_token(token)
    _auth = True
    today = datetime.today().strftime("%Y-%m-%d")
    with open(CONFIG["token_file"], "w") as f:
        json.dump({"token": token, "date": today}, f)
    _fetch_profile()
    add_log("Login successful", "info")


def _fetch_profile():
    try:
        p = kite.profile()
        state["profile"] = {
            "name":      p.get("user_name", ""),
            "email":     p.get("email", ""),
            "user_id":   p.get("user_id", ""),
            "broker":    p.get("broker", "ZERODHA"),
            "user_type": p.get("user_type", ""),
            "exchanges": p.get("exchanges", []),
            "products":  p.get("products", []),
        }
    except Exception as e:
        log.error(f"[PROFILE] {e}")

    try:
        margins = kite.margins()
        eq      = margins.get("equity", {})
        avail   = eq.get("available", {})
        used    = eq.get("utilised", {})
        state["funds"] = {
            "equity_available": round(float(avail.get("live_balance", 0)), 2),
            "equity_used":      round(float(used.get("debits", 0)), 2),
            "equity_total":     round(float(eq.get("net", 0)), 2),
            "opening_balance":  round(float(
                avail.get("opening_balance", 0)), 2),
        }
        net = state["funds"]["equity_total"]
        if net > 0:
            CONFIG["risk"]["total_capital"] = net
    except Exception as e:
        log.error(f"[FUNDS] {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET DATA — QUOTE API (faster + more reliable than historical)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_quotes_bulk(symbols: list, exchange: str = "NSE") -> dict:
    """
    Fetch live quotes for multiple symbols in ONE API call.
    Returns dict: {symbol: quote_data}
    Much faster than individual LTP calls.
    """
    if not _auth:
        return {}
    try:
        keys  = [f"{exchange}:{sym}" for sym in symbols]
        # Kite allows max 500 instruments per call
        chunk_size = 200
        result = {}
        for i in range(0, len(keys), chunk_size):
            chunk = keys[i:i + chunk_size]
            data  = kite.quote(chunk)
            for key, val in data.items():
                sym = key.split(":", 1)[1]
                result[sym] = val
                # Update LTP cache
                ltp = float(val.get("last_price", 0))
                state["ltp_cache"][sym] = ltp
        return result
    except Exception as e:
        log.error(f"[QUOTE BULK] {e}")
        return {}


def get_ltp(symbol: str, exchange: str = "NSE") -> float:
    """Fetch single LTP."""
    if not _auth:
        return float(state["ltp_cache"].get(symbol, 0.0))
    key = f"{exchange}:{symbol}"
    try:
        data  = kite.ltp([key])
        price = float(data[key]["last_price"])
        state["ltp_cache"][symbol] = price
        return price
    except Exception as e:
        log.error(f"[LTP] {symbol}: {e}")
        return float(state["ltp_cache"].get(symbol, 0.0))


def _get_instrument_token(symbol: str, exchange: str) -> int | None:
    """Get and cache instrument token."""
    key = f"{exchange}:{symbol}"
    if key in _token_cache:
        return _token_cache[key]
    try:
        instruments = kite.instruments(exchange)
        for i in instruments:
            if i["tradingsymbol"] == symbol:
                _token_cache[key] = i["instrument_token"]
                return i["instrument_token"]
    except Exception as e:
        log.error(f"[TOKEN] {symbol}: {e}")
    return None


def get_ohlcv(symbol: str, exchange: str,
              interval: str = "5minute",
              lookback: int = 100) -> pd.DataFrame:
    """Fetch OHLCV candles."""
    # Return cached if recent (within 5 min)
    cached = state["ohlcv_cache"].get(symbol)
    if isinstance(cached, dict):
        df   = cached.get("df", pd.DataFrame())
        ts   = cached.get("ts", 0)
        age  = time.time() - ts
        if not df.empty and age < 300:  # 5 min cache
            return df

    try:
        today = datetime.today().date()
        token = _get_instrument_token(symbol, exchange)
        if not token:
            return pd.DataFrame()

        candles = kite.historical_data(
            instrument_token = token,
            from_date        = today,
            to_date          = today,
            interval         = interval,
        )
        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(candles)
        df.columns = ["date", "open", "high", "low", "close", "volume"]
        df = df.tail(lookback).reset_index(drop=True)

        # Cache with timestamp
        state["ohlcv_cache"][symbol] = {"df": df, "ts": time.time()}
        return df

    except Exception as e:
        log.error(f"[OHLCV] {symbol}: {e}")
        cached = state["ohlcv_cache"].get(symbol, {})
        return cached.get("df", pd.DataFrame()) if isinstance(
            cached, dict) else pd.DataFrame()


def compute_indicators(symbol: str, exchange: str = "NSE") -> dict:
    """Compute all technical indicators for a symbol."""
    cfg = CONFIG["strategy"]
    df  = get_ohlcv(symbol, exchange,
                    interval=cfg["candle_interval"],
                    lookback=cfg["candle_lookback"])

    ltp = float(state["ltp_cache"].get(symbol, 0.0))

    if df.empty or len(df) < 10:
        log.warning(f"[IND] No OHLCV data for {symbol} — using quote only")
        # Return minimal indicators from LTP cache
        if ltp > 0:
            minimal = {
                "ltp":       ltp,
                "vwap":      ltp,   # approximate
                "ema9":      ltp,
                "ema20":     ltp,
                "ema200":    ltp,
                "rsi":       50.0,
                "atr":       ltp * 0.005,
                "macd":      {"macd": 0, "signal": 0, "histogram": 0},
                "bollinger": {
                    "upper":  ltp * 1.02,
                    "middle": ltp,
                    "lower":  ltp * 0.98,
                    "width":  4.0
                },
                "bias": "NEUTRAL",
            }
            state["indicator_cache"][symbol] = minimal
            return minimal
        return {}

    try:
        vwap   = calculate_vwap(df)
        ema9   = calculate_ema(df, cfg["ema_fast"])
        ema20  = calculate_ema(df, cfg["ema_slow"])
        ema200 = calculate_ema(df, cfg["ema_trend"]) \
                 if len(df) >= 50 else ema20
        rsi    = calculate_rsi(df, cfg["rsi_period"])
        atr    = calculate_atr(df, cfg["atr_period"])
        macd   = calculate_macd(df)
        boll   = calculate_bollinger(df)

        # Use actual LTP from cache (more real-time than last candle close)
        if ltp <= 0:
            ltp = float(df["close"].iloc[-1])

        bias = get_market_bias(ltp, vwap, ema20, ema200)

        indicators = {
            "ltp":       ltp,
            "vwap":      vwap,
            "ema9":      ema9,
            "ema20":     ema20,
            "ema200":    ema200,
            "rsi":       rsi,
            "atr":       atr,
            "macd":      macd,
            "bollinger": boll,
            "bias":      bias,
        }
        state["indicator_cache"][symbol] = indicators
        return indicators

    except Exception as e:
        log.error(f"[IND COMPUTE] {symbol}: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  DYNAMIC WATCHLIST
# ═══════════════════════════════════════════════════════════════════════════════

def build_dynamic_watchlist():
    """
    Fetch quotes for ALL NIFTY 50 stocks in one API call.
    Score each stock and return the best candidates.
    No hardcoded watchlist — bot picks dynamically.
    """
    if not _auth:
        return

    cfg      = CONFIG["strategy"]
    universe = NIFTY50_UNIVERSE
    size     = cfg.get("dynamic_wl_size", 20)

    add_log(f"Building dynamic watchlist from {len(universe)} stocks...",
            "info")

    try:
        # Fetch all quotes in one call
        quotes = fetch_quotes_bulk(universe, "NSE")
        if not quotes:
            add_log("Dynamic watchlist: no quote data", "alert")
            return

        scored = []
        for sym, q in quotes.items():
            try:
                ltp    = float(q.get("last_price", 0))
                volume = float(q.get("volume", 0))
                ohlc   = q.get("ohlc", {})
                open_p = float(ohlc.get("open", ltp))
                high   = float(ohlc.get("high", ltp))
                low    = float(ohlc.get("low",  ltp))
                close  = float(ohlc.get("close", ltp))

                if ltp <= 0:
                    continue
                if ltp < cfg.get("min_price", 100):
                    continue
                if volume < 100000:   # minimum liquidity
                    continue

                # Day change %
                day_chg = ((ltp - close) / close * 100) if close > 0 else 0
                # Intraday range %
                rng     = ((high - low) / low * 100) if low > 0 else 0
                # Volume score (higher = better)
                vol_score = min(volume / 1_000_000, 10)

                # Score = volatility + momentum + volume
                score = rng + abs(day_chg) + vol_score

                scored.append({
                    "symbol":   sym,
                    "exchange": "NSE",
                    "qty":      1,
                    "ltp":      ltp,
                    "volume":   volume,
                    "day_chg":  round(day_chg, 2),
                    "range":    round(rng, 2),
                    "score":    round(score, 4),
                    "priority": 1,
                })
            except Exception as ex:
                log.debug(f"[DYN WL] {sym}: {ex}")

        # Sort by score — best candidates first
        scored.sort(key=lambda x: x["score"], reverse=True)

        # Take top N
        top = scored[:size]
        for i, item in enumerate(top):
            item["priority"] = i + 1

        # Update state
        state["dynamic_watchlist"] = top

        # Also update CONFIG watchlist so paper trader uses it
        CONFIG["watchlist"] = [
            {
                "symbol":   x["symbol"],
                "exchange": x["exchange"],
                "qty":      x["qty"],
                "priority": x["priority"],
            }
            for x in top
        ]

        symbols = [x["symbol"] for x in top[:5]]
        add_log(
            f"Dynamic watchlist updated: {len(top)} stocks | "
            f"Top 5: {', '.join(symbols)}", "info"
        )

    except Exception as e:
        log.error(f"[DYN WATCHLIST] {e}")
        add_log(f"Dynamic watchlist failed: {e}", "alert")


# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET REGIME
# ═══════════════════════════════════════════════════════════════════════════════

def update_market_regime():
    """Compute NIFTY 50 bias."""
    idx = CONFIG["index"]
    ind = compute_indicators(idx["symbol"], idx["exchange"])
    if not ind:
        state["market_bias"] = "NEUTRAL"
        return
    state["market_bias"] = ind["bias"]
    add_log(
        f"Market Regime: {ind['bias']} | "
        f"NIFTY:{fmt_price(ind['ltp'])} "
        f"VWAP:{fmt_price(ind['vwap'])} "
        f"EMA20:{fmt_price(ind['ema20'])}",
        "info"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def place_order(symbol: str, exchange: str, side: str, qty: int,
                order_type: str = "MARKET", price: float = 0,
                sl_price: float = 0, tp_price: float = 0) -> str | None:
    try:
        transaction = (kite.TRANSACTION_TYPE_BUY
                       if side == "BUY"
                       else kite.TRANSACTION_TYPE_SELL)
        otype = (kite.ORDER_TYPE_MARKET
                 if order_type == "MARKET"
                 else kite.ORDER_TYPE_LIMIT)

        params = {
            "variety":          kite.VARIETY_REGULAR,
            "exchange":         exchange,
            "tradingsymbol":    symbol,
            "transaction_type": transaction,
            "quantity":         qty,
            "order_type":       otype,
            "product":          kite.PRODUCT_MIS,
            "validity":         kite.VALIDITY_DAY,
        }
        if order_type == "LIMIT":
            params["price"] = round(price, 2)

        order_id = kite.place_order(**params)
        ltp = get_ltp_cached(symbol)
        add_log(
            f"{side} {qty}×{symbol} @ {fmt_price(price or ltp)} "
            f"SL:{fmt_price(sl_price)} TP:{fmt_price(tp_price)} "
            f"[{order_id}]",
            "buy" if side == "BUY" else "sell"
        )
        _log_trade(symbol, side, qty, price or ltp,
                   order_id, sl_price, tp_price)
        state["performance"]["brokerage_paid"] += 20
        return str(order_id)

    except Exception as e:
        add_log(f"ORDER FAILED {symbol} ({side}): {e}", "alert")
        log.error(f"[ORDER] {symbol} {side}: {e}")
        return None


def _log_trade(symbol, side, qty, price, order_id, sl=0, tp=0):
    entry = {
        "timestamp":   datetime.now().isoformat(),
        "symbol":      symbol,
        "side":        side,
        "qty":         qty,
        "price":       round(float(price), 2),
        "order_id":    str(order_id),
        "sl":          round(float(sl), 2),
        "tp":          round(float(tp), 2),
        "market_bias": state.get("market_bias", ""),
    }
    try:
        with open(CONFIG["trade_log"], "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  HYBRID STRATEGY
# ═══════════════════════════════════════════════════════════════════════════════

def run_hybrid_strategy():
    bias = state["market_bias"]
    cfg  = CONFIG["strategy"]

    if bias not in ("BULL", "STRONG_BULL"):
        add_log(f"Regime filter: {bias} — skipping entries", "info")
        return

    watchlist = get_active_watchlist()
    add_log(f"Scanning {len(watchlist)} stocks...", "info")

    for item in sorted(watchlist, key=lambda x: x.get("priority", 9)):
        symbol   = item["symbol"]
        exchange = item.get("exchange", "NSE")

        ind = compute_indicators(symbol, exchange)
        if not ind or ind.get("ltp", 0) <= 0:
            continue

        ltp  = float(ind["ltp"])
        vwap = float(ind["vwap"])
        ema9 = float(ind["ema9"])
        rsi  = float(ind["rsi"])
        atr  = float(ind["atr"])
        macd = ind["macd"]
        boll = ind["bollinger"]

        # ── ENTRY ─────────────────────────────────────────────────────────────
        if symbol not in state["open_positions"]:
            ok, reason = risk_mgr.can_trade(symbol)
            if not ok:
                continue

            if vwap <= 0:
                continue

            vwap_diff = abs(ltp - vwap) / vwap
            near_vwap = vwap_diff <= cfg["vwap_entry_buffer"]
            at_vwap   = ltp <= vwap * 1.001
            rsi_ok    = rsi < cfg["rsi_overbought"]
            boll_ok   = ltp >= float(boll.get("lower", 0))

            if near_vwap and at_vwap and rsi_ok and boll_ok:
                if cfg["use_dynamic_sizing"]:
                    qty = calculate_position_size(
                        capital     = CONFIG["risk"]["total_capital"],
                        risk_pct    = cfg["risk_per_trade_pct"],
                        entry_price = ltp,
                        sl_pct      = cfg["stop_loss_pct"],
                        min_qty     = item.get("qty", 1),
                    )
                else:
                    qty = item.get("qty", 1)

                sl_price = round(ltp * (1 - cfg["stop_loss_pct"] / 100), 2)
                tp_price = round(ltp * (1 + cfg["take_profit_pct"] / 100), 2)

                add_log(
                    f"ENTRY {symbol} | LTP:{fmt_price(ltp)} "
                    f"VWAP:{fmt_price(vwap)} RSI:{rsi:.1f} Qty:{qty}",
                    "buy"
                )
                order_id = place_order(
                    symbol, exchange, "BUY", qty,
                    sl_price=sl_price, tp_price=tp_price)

                if order_id:
                    with _state_lock:
                        state["open_positions"][symbol] = {
                            "entry_price": ltp,
                            "qty":         qty,
                            "side":        "BUY",
                            "order_id":    order_id,
                            "exchange":    exchange,
                            "sl_price":    sl_price,
                            "tp_price":    tp_price,
                            "trail_sl":    sl_price,
                            "entry_time":  datetime.now().isoformat(),
                            "entry_vwap":  vwap,
                            "entry_rsi":   rsi,
                            "atr":         atr,
                        }
                        state["trade_count"] += 1
                        state["performance"]["total_trades"] += 1

        # ── EXIT ──────────────────────────────────────────────────────────────
        else:
            pos     = state["open_positions"][symbol]
            entry   = float(pos["entry_price"])
            qty     = int(pos["qty"])
            atr_val = float(pos.get("atr", 0))
            pnl_pct = (ltp - entry) / entry * 100

            new_trail = calculate_trailing_sl(entry, ltp, atr_val, "BUY")
            if new_trail > pos["trail_sl"]:
                pos["trail_sl"] = new_trail
                add_log(f"TRAIL SL → {symbol} {fmt_price(new_trail)}", "info")

            should_exit = False
            exit_reason = ""

            if pnl_pct >= cfg["take_profit_pct"]:
                should_exit = True
                exit_reason = f"TAKE PROFIT +{pnl_pct:.2f}%"
            elif ltp <= pos["trail_sl"]:
                should_exit = True
                exit_reason = f"TRAIL SL @ {fmt_price(pos['trail_sl'])}"
            elif pnl_pct <= -cfg["stop_loss_pct"]:
                should_exit = True
                exit_reason = f"STOP LOSS {pnl_pct:.2f}%"
            elif rsi >= 78:
                should_exit = True
                exit_reason = f"RSI overbought ({rsi:.1f})"
            elif ltp < ema9 and pnl_pct > 0:
                should_exit = True
                exit_reason = f"EMA9 breakdown @ {fmt_price(ema9)}"

            if should_exit:
                add_log(
                    f"EXIT {symbol} | {exit_reason} | "
                    f"Entry:{fmt_price(entry)} LTP:{fmt_price(ltp)} "
                    f"PnL:{pnl_pct:+.2f}%",
                    "sell"
                )
                _exit_position(symbol, exchange, qty, ltp, entry, exit_reason)


def _exit_position(symbol, exchange, qty, ltp, entry, reason=""):
    order_id = place_order(symbol, exchange, "SELL", qty)
    if order_id:
        pnl    = round((ltp - entry) * qty, 2)
        is_win = pnl > 0
        with _state_lock:
            state["daily_pnl"]  += pnl
            state["trade_count"] += 1
            perf = state["performance"]
            perf["gross_pnl"]    += pnl
            perf["total_trades"] += 1
            if is_win:
                perf["wins"]      += 1
                perf["best_trade"] = max(perf["best_trade"], pnl)
            else:
                perf["losses"]     += 1
                perf["worst_trade"] = min(perf["worst_trade"], pnl)
                risk_mgr.add_strike(symbol)
            if symbol in state["open_positions"]:
                del state["open_positions"][symbol]

        add_log(
            f"CLOSED {symbol} | PnL:{fmt_price(pnl)} | "
            f"Daily:{fmt_price(state['daily_pnl'])} | {reason}",
            "buy" if is_win else "alert"
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  EOD SQUARE OFF
# ═══════════════════════════════════════════════════════════════════════════════

def square_off_all(reason: str = "EOD"):
    add_log(f"SQUARE OFF ALL — {reason}", "alert")
    for symbol, pos in list(state["open_positions"].items()):
        try:
            ltp   = float(state["ltp_cache"].get(symbol, pos["entry_price"]))
            entry = float(pos["entry_price"])
            pnl   = round((ltp - entry) * int(pos["qty"]), 2)
            place_order(symbol, pos.get("exchange", "NSE"),
                        "SELL", pos["qty"])
            state["daily_pnl"] += pnl
            del state["open_positions"][symbol]
            add_log(f"SQ OFF {symbol} PnL:{fmt_price(pnl)}", "alert")
        except Exception as e:
            log.error(f"[SQ OFF] {symbol}: {e}")
    add_log("All positions squared off.", "alert")


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY TICK
# ═══════════════════════════════════════════════════════════════════════════════

def _strategy_tick():
    if not is_market_open() or not _auth:
        return

    now   = datetime.now().time()
    today = datetime.today().strftime("%Y-%m-%d")

    if state["session_date"] != today:
        risk_mgr.daily_reset()
        state["session_date"] = today
        add_log("New session — daily counters reset", "info")

    if now < dtime(9, 30):
        add_log("No-fly zone (9:15–9:30) — observing", "info")
        return

    try:
        _fetch_profile()
    except Exception:
        pass

    risk_mgr.check_mtm(state["ltp_cache"])
    update_market_regime()

    mode = CONFIG["strategy"]["mode"]
    add_log(
        f"TICK | Mode:{mode} | Bias:{state['market_bias']} | "
        f"PnL:{fmt_price(state['daily_pnl'])} | "
        f"Trades:{state['trade_count']} | "
        f"WL:{len(get_active_watchlist())} stocks",
        "info"
    )

    if mode in ("hybrid", "margin"):
        run_hybrid_strategy()


# ═══════════════════════════════════════════════════════════════════════════════
#  BOT THREAD
# ═══════════════════════════════════════════════════════════════════════════════

def start_bot():
    global bot_running
    bot_running = True
    _stop_event.clear()

    import schedule
    schedule.clear()
    schedule.every(60).seconds.do(_strategy_tick)
    schedule.every(15).minutes.do(build_dynamic_watchlist)
    schedule.every().day.at("15:15").do(
        lambda: square_off_all("EOD 3:15 PM"))
    schedule.every().day.at("09:16").do(build_dynamic_watchlist)
    schedule.every().day.at("09:30").do(update_market_regime)

    add_log("Bot STARTED — Dynamic VWAP-EMA Hybrid", "info")

    while not _stop_event.is_set():
        schedule.run_pending()
        time.sleep(1)

    bot_running = False


def stop_bot():
    global bot_running
    _stop_event.set()
    bot_running = False
    import schedule
    schedule.clear()
    add_log("Bot STOPPED", "alert")
