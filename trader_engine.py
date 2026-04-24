"""
Robust VWAP-EMA Hybrid Trading Engine
Strategy: Trend-Confirmed Mean Reversion with Dynamic Position Sizing

Layers:
  1. Market Regime Filter (NIFTY VWAP + EMA)
  2. VWAP Pullback Entry with RSI + Bollinger confirmation
  3. ATR-based Trailing Stop Loss
  4. Z-Score Arbitrage (only on extreme dislocations)
  5. Risk Manager with Two-Strike Rule + Circuit Breaker
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

# ─── Load .env file ───────────────────────────────────────────────────────────
from dotenv import load_dotenv

# Load .env from same directory as this file
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# Read credentials from environment
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# Validate — crash early if missing
if not API_KEY or not API_SECRET:
    raise EnvironmentError(
        "\n[ERROR] API_KEY or API_SECRET not found in .env file.\n"
        "Please create a .env file with:\n"
        "  API_KEY=your_api_key\n"
        "  API_SECRET=your_api_secret\n"
    )

# ─── Logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
log.info(f"[ENV] API Key loaded: {API_KEY[:6]}{'*' * (len(API_KEY) - 6)}")

from indicators import (
    calculate_vwap, calculate_ema, calculate_ema_series,
    calculate_atr, calculate_rsi, calculate_macd,
    calculate_bollinger, calculate_zscore,
    get_market_bias, calculate_position_size, calculate_trailing_sl
)
from risk_manager import RiskManager

# ─── CONFIG ───────────────────────────────────────────────────────────────────
CONFIG = {
    # Loaded securely from .env file
    "api_key":    API_KEY,
    "api_secret": API_SECRET,

    "strategy": {
        "mode":                "hybrid",
        "margin_pct":          0.5,
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
        "no_fly_minutes":      15,
        "trade_cutoff_hour":   14,
        "trade_cutoff_minute": 0,
    },

    "watchlist": [
        {"symbol": "RELIANCE",  "exchange": "NSE", "qty": 1, "priority": 1},
        {"symbol": "HDFCBANK",  "exchange": "NSE", "qty": 1, "priority": 1},
        {"symbol": "ICICIBANK", "exchange": "NSE", "qty": 1, "priority": 2},
        {"symbol": "INFY",      "exchange": "NSE", "qty": 1, "priority": 2},
        {"symbol": "TCS",       "exchange": "NSE", "qty": 1, "priority": 3},
    ],

    "index": {
        "symbol":   "NIFTY 50",
        "exchange": "NSE",
    },

    "arb_pairs": [
        {
            "leg_a": {"symbol": "NIFTY25MAYFUT", "exchange": "NFO"},
            "leg_b": {"symbol": "NIFTY 50",      "exchange": "NSE"},
            "spread_threshold": 50,
            "zscore_entry":     2.5,
            "zscore_exit":      0.5,
            "qty":              50,
            "spread_history":   [],
        }
    ],

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

# ─── STATE ────────────────────────────────────────────────────────────────────
state = {
    "open_positions":   {},
    "daily_pnl":        0.0,
    "mtm_pnl":          0.0,
    "trade_count":      0,
    "halted":           False,
    "market_stop":      False,
    "ltp_cache":        {},
    "ohlcv_cache":      {},
    "indicator_cache":  {},
    "reference_prices": {},
    "activity_log":     [],
    "profile":          {},
    "funds":            {},
    "strike_count":     {},
    "market_bias":      "NEUTRAL",
    "session_date":     "",
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

# ─── KITE CLIENT ──────────────────────────────────────────────────────────────
kite        = KiteConnect(api_key=CONFIG["api_key"])
_auth       = False
bot_running = False
_stop_event = threading.Event()
risk_mgr    = RiskManager(CONFIG, state)


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def is_authenticated() -> bool:
    return _auth


def is_market_open() -> bool:
    now = datetime.now().time()
    return CONFIG["market_open"] <= now <= CONFIG["market_close"]


def get_ltp_cached(symbol: str) -> float:
    return state["ltp_cache"].get(symbol, 0.0)


def add_log(msg: str, log_type: str = "info"):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "msg":  msg,
        "type": log_type
    }
    state["activity_log"].append(entry)
    if len(state["activity_log"]) > 300:
        state["activity_log"] = state["activity_log"][-300:]
    log.info(f"[{log_type.upper()}] {msg}")


def fmt_price(p: float) -> str:
    return f"Rs{p:,.2f}"


# ─── AUTH ─────────────────────────────────────────────────────────────────────
def try_load_token():
    """Try to load saved token silently on startup."""
    global _auth
    try:
        with open(CONFIG["token_file"]) as f:
            data = json.load(f)
        today = datetime.today().strftime("%Y-%m-%d")
        if data.get("date") == today:
            kite.set_access_token(data["token"])
            _auth = True
            _fetch_profile()
            add_log("Access token loaded from file", "info")
            log.info("[AUTH] Token loaded.")
        else:
            log.info("[AUTH] Stale token. Login required.")
    except FileNotFoundError:
        log.info("[AUTH] No token file. Login required.")
    except Exception as e:
        log.error(f"[AUTH] Error: {e}")


def authenticate(request_token: str):
    """Exchange request_token for access_token."""
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
    add_log("Login successful — token saved", "info")


def _fetch_profile():
    """Fetch user profile and margin data from Kite."""
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
        log.info(f"[PROFILE] Loaded: {state['profile']['name']}")
    except Exception as e:
        log.error(f"[PROFILE] {e}")

    try:
        margins = kite.margins()
        eq      = margins.get("equity", {})
        avail   = eq.get("available", {})
        used    = eq.get("utilised", {})
        state["funds"] = {
            "equity_available": round(avail.get("live_balance", 0), 2),
            "equity_used":      round(used.get("debits", 0), 2),
            "equity_total":     round(eq.get("net", 0), 2),
            "opening_balance":  round(avail.get("opening_balance", 0), 2),
        }
        net = state["funds"]["equity_total"]
        if net > 0:
            CONFIG["risk"]["total_capital"] = net
    except Exception as e:
        log.error(f"[FUNDS] {e}")


# ─── MARKET DATA ──────────────────────────────────────────────────────────────
def get_ltp(symbol: str, exchange: str) -> float:
    """Fetch live LTP and update cache."""
    key = f"{exchange}:{symbol}"
    try:
        data  = kite.ltp([key])
        price = data[key]["last_price"]
        state["ltp_cache"][symbol] = price
        return price
    except Exception as e:
        log.error(f"LTP failed {symbol}: {e}")
        return state["ltp_cache"].get(symbol, 0.0)


def get_ohlcv(symbol: str, exchange: str,
              interval: str = "5minute",
              lookback: int = 100) -> pd.DataFrame:
    """
    Fetch OHLCV candles from Kite historical API.
    Returns DataFrame: date, open, high, low, close, volume
    """
    try:
        today     = datetime.today().date()
        from_date = today
        to_date   = today

        instruments = kite.instruments(exchange)
        token = next(
            (i["instrument_token"] for i in instruments
             if i["tradingsymbol"] == symbol), None
        )
        if not token:
            log.error(f"[OHLCV] Token not found for {symbol}")
            return pd.DataFrame()

        candles = kite.historical_data(
            instrument_token=token,
            from_date=from_date,
            to_date=to_date,
            interval=interval
        )

        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(candles)
        df.columns = ["date", "open", "high", "low", "close", "volume"]
        df = df.tail(lookback).reset_index(drop=True)
        state["ohlcv_cache"][symbol] = df
        return df

    except Exception as e:
        log.error(f"[OHLCV] Failed for {symbol}: {e}")
        return state["ohlcv_cache"].get(symbol, pd.DataFrame())


def compute_indicators(symbol: str, exchange: str) -> dict:
    """Compute all indicators for a symbol."""
    cfg = CONFIG["strategy"]
    df  = get_ohlcv(symbol, exchange,
                    interval=cfg["candle_interval"],
                    lookback=cfg["candle_lookback"])

    if df.empty or len(df) < 20:
        log.warning(f"[INDICATORS] Insufficient data for {symbol}")
        return {}

    ltp    = get_ltp(symbol, exchange)
    vwap   = calculate_vwap(df)
    ema9   = calculate_ema(df, cfg["ema_fast"])
    ema20  = calculate_ema(df, cfg["ema_slow"])
    ema200 = calculate_ema(df, cfg["ema_trend"]) if len(df) >= 50 else ema20
    rsi    = calculate_rsi(df, cfg["rsi_period"])
    atr    = calculate_atr(df, cfg["atr_period"])
    macd   = calculate_macd(df)
    boll   = calculate_bollinger(df)
    bias   = get_market_bias(ltp, vwap, ema20, ema200)

    indicators = {
        "ltp": ltp, "vwap": vwap,
        "ema9": ema9, "ema20": ema20, "ema200": ema200,
        "rsi": rsi, "atr": atr,
        "macd": macd, "bollinger": boll, "bias": bias,
    }
    state["indicator_cache"][symbol] = indicators
    return indicators


# ─── MARKET REGIME ────────────────────────────────────────────────────────────
def update_market_regime():
    """Check NIFTY 50 to determine overall market bias."""
    idx    = CONFIG["index"]
    ind    = compute_indicators(idx["symbol"], idx["exchange"])
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


# ─── ORDER PLACEMENT ──────────────────────────────────────────────────────────
def place_order(symbol: str, exchange: str, side: str, qty: int,
                order_type: str = "MARKET", price: float = 0,
                sl_price: float = 0, tp_price: float = 0) -> str | None:
    """Place buy or sell order via Kite API."""
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
        msg = (f"{side} {qty}x {symbol} @ "
               f"{fmt_price(price or get_ltp_cached(symbol))} "
               f"SL:{fmt_price(sl_price)} TP:{fmt_price(tp_price)} "
               f"| ID:{order_id}")
        add_log(msg, "buy" if side == "BUY" else "sell")
        _log_trade(symbol, side, qty, price, order_id, sl_price, tp_price)
        state["performance"]["brokerage_paid"] += 20
        return str(order_id)

    except Exception as e:
        add_log(f"ORDER FAILED {symbol} ({side}): {e}", "alert")
        return None


def _log_trade(symbol, side, qty, price, order_id, sl=0, tp=0):
    """Append trade to trades.json."""
    entry = {
        "timestamp":   datetime.now().isoformat(),
        "symbol":      symbol,
        "side":        side,
        "qty":         qty,
        "price":       price,
        "order_id":    order_id,
        "sl":          sl,
        "tp":          tp,
        "market_bias": state.get("market_bias", ""),
    }
    try:
        with open(CONFIG["trade_log"], "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ─── HYBRID STRATEGY ──────────────────────────────────────────────────────────
def run_hybrid_strategy():
    """VWAP + EMA Hybrid — Entry and Exit Logic."""
    bias = state["market_bias"]
    cfg  = CONFIG["strategy"]

    if bias not in ("BULL", "STRONG_BULL"):
        add_log(f"Regime filter: {bias} — no long trades", "info")
        return

    for item in sorted(CONFIG["watchlist"],
                        key=lambda x: x.get("priority", 9)):
        symbol   = item["symbol"]
        exchange = item["exchange"]

        ind = compute_indicators(symbol, exchange)
        if not ind or ind["ltp"] == 0:
            continue

        ltp  = ind["ltp"]
        vwap = ind["vwap"]
        ema9 = ind["ema9"]
        rsi  = ind["rsi"]
        atr  = ind["atr"]
        macd = ind["macd"]
        boll = ind["bollinger"]

        # ── ENTRY ─────────────────────────────────────────────────────────
        if symbol not in state["open_positions"]:
            ok, reason = risk_mgr.can_trade(symbol)
            if not ok:
                log.debug(f"[SKIP] {symbol}: {reason}")
                continue

            vwap_diff = abs(ltp - vwap) / vwap
            near_vwap = vwap_diff <= cfg["vwap_entry_buffer"]
            at_vwap   = ltp <= vwap * 1.001
            rsi_ok    = rsi < cfg["rsi_overbought"]
            boll_ok   = ltp >= boll["lower"]

            if near_vwap and at_vwap and rsi_ok and boll_ok:
                if cfg["use_dynamic_sizing"]:
                    qty = calculate_position_size(
                        capital     = CONFIG["risk"]["total_capital"],
                        risk_pct    = cfg["risk_per_trade_pct"],
                        entry_price = ltp,
                        sl_pct      = cfg["stop_loss_pct"],
                        min_qty     = item.get("qty", 1)
                    )
                else:
                    qty = item.get("qty", cfg["order_quantity"])

                sl_price = round(ltp * (1 - cfg["stop_loss_pct"] / 100), 2)
                tp_price = round(ltp * (1 + cfg["take_profit_pct"] / 100), 2)

                add_log(
                    f"ENTRY: {symbol} | LTP:{fmt_price(ltp)} "
                    f"VWAP:{fmt_price(vwap)} RSI:{rsi:.1f} "
                    f"MACD:{macd['histogram']:.4f} Qty:{qty}",
                    "buy"
                )

                order_id = place_order(symbol, exchange, "BUY", qty,
                                       sl_price=sl_price, tp_price=tp_price)
                if order_id:
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

        # ── EXIT ──────────────────────────────────────────────────────────
        else:
            pos     = state["open_positions"][symbol]
            entry   = pos["entry_price"]
            qty     = pos["qty"]
            atr     = pos.get("atr", 0)
            pnl_pct = (ltp - entry) / entry * 100

            new_trail = calculate_trailing_sl(entry, ltp, atr, "BUY")
            if new_trail > pos["trail_sl"]:
                pos["trail_sl"] = new_trail
                add_log(
                    f"TRAIL SL updated: {symbol} "
                    f"-> {fmt_price(new_trail)}", "info"
                )

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
                exit_reason = f"EMA9 breakdown {fmt_price(ema9)}"

            if should_exit:
                add_log(
                    f"EXIT: {symbol} | {exit_reason} | "
                    f"Entry:{fmt_price(entry)} LTP:{fmt_price(ltp)} "
                    f"PnL:{pnl_pct:+.2f}%",
                    "sell"
                )
                _exit_position(symbol, exchange, qty, ltp, entry, exit_reason)


def _exit_position(symbol, exchange, qty, ltp, entry, reason=""):
    """Execute sell order and update performance state."""
    order_id = place_order(symbol, exchange, "SELL", qty)
    if order_id:
        pnl    = round((ltp - entry) * qty, 2)
        is_win = pnl > 0
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

        del state["open_positions"][symbol]
        add_log(
            f"CLOSED {symbol} | PnL:{fmt_price(pnl)} | "
            f"Daily:{fmt_price(state['daily_pnl'])} | {reason}",
            "buy" if is_win else "alert"
        )


# ─── Z-SCORE ARBITRAGE ────────────────────────────────────────────────────────
def run_arbitrage_strategy():
    """Z-Score Cash-Futures Arbitrage."""
    for pair in CONFIG["arb_pairs"]:
        leg_a    = pair["leg_a"]
        leg_b    = pair["leg_b"]
        qty      = pair["qty"]
        pair_key = f"ARB_{leg_a['symbol']}_{leg_b['symbol']}"

        price_a = get_ltp(leg_a["symbol"], leg_a["exchange"])
        price_b = get_ltp(leg_b["symbol"], leg_b["exchange"])

        if price_a == 0 or price_b == 0:
            continue

        spread  = price_a - price_b
        history = pair["spread_history"]
        history.append(spread)
        if len(history) > 100:
            history.pop(0)

        if len(history) < 20:
            add_log(f"ARB: Building history ({len(history)}/20)", "info")
            continue

        zscore = calculate_zscore(pd.Series(history))
        add_log(
            f"ARB | Spread:{fmt_price(spread)} Z:{zscore:.2f}", "info"
        )

        if (zscore >= pair["zscore_entry"]
                and pair_key not in state["open_positions"]):
            ok, reason = risk_mgr.can_trade()
            if ok:
                add_log(f"ARB ENTRY: Z={zscore:.2f}", "buy")
                oid_a = place_order(
                    leg_a["symbol"], leg_a["exchange"], "SELL", qty)
                oid_b = place_order(
                    leg_b["symbol"], leg_b["exchange"], "BUY", qty)
                if oid_a and oid_b:
                    state["open_positions"][pair_key] = {
                        "entry_spread":  spread,
                        "entry_zscore":  zscore,
                        "qty":           qty,
                        "leg_a":         leg_a,
                        "leg_b":         leg_b,
                        "entry_price_a": price_a,
                        "entry_price_b": price_b,
                    }
                    state["trade_count"] += 2

        elif pair_key in state["open_positions"]:
            pos = state["open_positions"][pair_key]
            if zscore <= pair["zscore_exit"]:
                add_log(f"ARB EXIT: Z={zscore:.2f}", "sell")
                place_order(leg_a["symbol"], leg_a["exchange"], "BUY",  qty)
                place_order(leg_b["symbol"], leg_b["exchange"], "SELL", qty)
                pnl = (pos["entry_spread"] - spread) * qty
                state["daily_pnl"]   += pnl
                state["trade_count"] += 2
                del state["open_positions"][pair_key]
                add_log(
                    f"ARB CLOSED: PnL {fmt_price(pnl)}",
                    "buy" if pnl > 0 else "alert"
                )


# ─── EOD SQUARE OFF ───────────────────────────────────────────────────────────
def square_off_all(reason: str = "EOD"):
    """Force close all positions at 3:15 PM."""
    add_log(f"SQUARE OFF ALL — {reason}", "alert")
    for symbol, pos in list(state["open_positions"].items()):
        if symbol.startswith("ARB_"):
            la = pos["leg_a"]
            lb = pos["leg_b"]
            place_order(la["symbol"], la["exchange"], "BUY",  pos["qty"])
            place_order(lb["symbol"], lb["exchange"], "SELL", pos["qty"])
        else:
            ltp   = get_ltp_cached(symbol)
            entry = pos["entry_price"]
            pnl   = round((ltp - entry) * pos["qty"], 2)
            place_order(symbol, pos["exchange"], "SELL", pos["qty"])
            state["daily_pnl"] += pnl
        del state["open_positions"][symbol]
    add_log("All positions closed.", "alert")


# ─── STRATEGY TICK ────────────────────────────────────────────────────────────
def _strategy_tick():
    """Main tick — runs every 60 seconds."""
    if not is_market_open() or not _auth:
        return

    now   = datetime.now().time()
    today = datetime.today().strftime("%Y-%m-%d")

    # Daily reset
    if state["session_date"] != today:
        risk_mgr.daily_reset()
        state["session_date"] = today
        add_log("New session — daily state reset", "info")

    # No-fly zone
    if now < dtime(9, 30):
        add_log("No-fly zone (9:15-9:30) — observing", "info")
        return

    try:
        _fetch_profile()
    except Exception:
        pass

    risk_mgr.check_mtm(state["ltp_cache"])

    mode = CONFIG["strategy"]["mode"]
    add_log(
        f"TICK | Mode:{mode} | Bias:{state['market_bias']} | "
        f"PnL:{fmt_price(state['daily_pnl'])} | "
        f"MTM:{fmt_price(state['mtm_pnl'])} | "
        f"Trades:{state['trade_count']}",
        "info"
    )

    update_market_regime()

    if mode in ("hybrid", "margin"):
        run_hybrid_strategy()

    if mode in ("hybrid", "arbitrage"):
        run_arbitrage_strategy()


# ─── BOT THREAD ───────────────────────────────────────────────────────────────
def start_bot():
    global bot_running
    bot_running = True
    _stop_event.clear()

    import schedule
    schedule.clear()
    schedule.every(60).seconds.do(_strategy_tick)
    schedule.every().day.at("15:15").do(
        lambda: square_off_all("EOD 3:15 PM"))
    schedule.every().day.at("09:30").do(update_market_regime)

    add_log("Bot started — VWAP-EMA Hybrid active", "info")

    while not _stop_event.is_set():
        schedule.run_pending()
        time.sleep(1)

    bot_running = False
    log.info("[BOT] Stopped.")


def stop_bot():
    global bot_running
    _stop_event.set()
    bot_running = False
    import schedule
    schedule.clear()
    add_log("Bot stopped by user", "alert")
    log.info("[BOT] Stop requested.")