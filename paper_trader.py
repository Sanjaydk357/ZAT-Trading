"""
Paper Trading Engine — Simulates live trades using real market data.
Runs in parallel with the live bot. Fully isolated state.
"""
import json
import logging
import threading
from datetime import datetime, time as dtime
from indicators import (
    calculate_vwap, calculate_ema, calculate_atr, calculate_rsi,
    calculate_macd, calculate_bollinger, get_market_bias,
    calculate_position_size, calculate_trailing_sl
)

log = logging.getLogger(__name__)

# ─── DEFAULT PAPER CONFIG ────────────────────────────────────────────────────
PAPER_DEFAULTS = {
    "enabled":            False,
    "starting_capital":   200000.0,
    "current_capital":    200000.0,
    "mode":               "hybrid",
    "take_profit_pct":    1.5,
    "stop_loss_pct":      0.35,
    "max_trades_per_day": 6,
    "rsi_oversold":       45,
    "rsi_overbought":     70,
    "vwap_entry_buffer":  0.0015,
    "risk_per_trade_pct": 0.5,
    "use_dynamic_sizing": True,
    "trailing_sl":        True,
    "max_daily_loss":     3000,
    "max_open_positions": 3,
    "brokerage_per_order": 20,   # Zerodha flat ₹20/order
}

# ─── PAPER STATE ─────────────────────────────────────────────────────────────
paper_state = {
    "enabled":          False,
    "open_positions":   {},
    "daily_pnl":        0.0,
    "mtm_pnl":          0.0,
    "trade_count":      0,
    "halted":           False,
    "activity_log":     [],
    "strike_count":     {},
    "market_bias":      "NEUTRAL",
    "session_date":     "",
    "config":           dict(PAPER_DEFAULTS),
    "performance": {
        "total_trades":    0,
        "wins":            0,
        "losses":          0,
        "gross_pnl":       0.0,
        "brokerage_paid":  0.0,
        "best_trade":      0.0,
        "worst_trade":     0.0,
        "starting_capital": 200000.0,
        "current_capital":  200000.0,
    },
    "trade_history":    [],   # Paper trade log (in-memory)
    "order_counter":    1,
}

_lock = threading.Lock()

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def add_paper_log(msg: str, log_type: str = "info"):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "msg":  f"[PAPER] {msg}",
        "type": log_type,
    }
    with _lock:
        paper_state["activity_log"].append(entry)
        if len(paper_state["activity_log"]) > 300:
            paper_state["activity_log"] = paper_state["activity_log"][-300:]
    log.info(f"[PAPER][{log_type.upper()}] {msg}")

def fmt(p: float) -> str:
    return f"₹{p:,.2f}"

def next_order_id() -> str:
    with _lock:
        oid = f"PAPER-{paper_state['order_counter']:04d}"
        paper_state["order_counter"] += 1
    return oid

# ─── PAPER TRADING CONTROLS ──────────────────────────────────────────────────
def enable_paper(config_override: dict = None):
    """Enable paper trading with optional config overrides."""
    with _lock:
        paper_state["enabled"] = True
        cfg = paper_state["config"]
        if config_override:
            cfg.update(config_override)
        # Reset daily state on enable
        _reset_daily_state()
    add_paper_log("Paper trading ENABLED — simulating with real market data", "alert")

def disable_paper():
    with _lock:
        paper_state["enabled"] = False
    add_paper_log("Paper trading DISABLED", "alert")

def reset_paper():
    """Full reset of paper trading state (new session)."""
    with _lock:
        starting = paper_state["config"].get("starting_capital", 200000.0)
        paper_state["open_positions"]   = {}
        paper_state["daily_pnl"]        = 0.0
        paper_state["mtm_pnl"]          = 0.0
        paper_state["trade_count"]      = 0
        paper_state["halted"]           = False
        paper_state["activity_log"]     = []
        paper_state["strike_count"]     = {}
        paper_state["market_bias"]      = "NEUTRAL"
        paper_state["session_date"]     = ""
        paper_state["trade_history"]    = []
        paper_state["order_counter"]    = 1
        paper_state["performance"] = {
            "total_trades":    0,
            "wins":            0,
            "losses":          0,
            "gross_pnl":       0.0,
            "brokerage_paid":  0.0,
            "best_trade":      0.0,
            "worst_trade":     0.0,
            "starting_capital": starting,
            "current_capital":  starting,
        }
        paper_state["config"]["current_capital"] = starting
    add_paper_log(f"Paper state fully RESET — capital ₹{starting:,.0f}", "alert")

def _reset_daily_state():
    paper_state["daily_pnl"]    = 0.0
    paper_state["trade_count"]  = 0
    paper_state["halted"]       = False
    paper_state["strike_count"] = {}
    paper_state["open_positions"] = {}

def update_config(data: dict):
    """Update paper trading config from dashboard."""
    with _lock:
        cfg = paper_state["config"]
        cfg["mode"]               = str(data.get("mode", cfg["mode"]))
        cfg["take_profit_pct"]    = float(data.get("take_profit_pct", cfg["take_profit_pct"]))
        cfg["stop_loss_pct"]      = float(data.get("stop_loss_pct", cfg["stop_loss_pct"]))
        cfg["max_trades_per_day"] = int(data.get("max_trades_per_day", cfg["max_trades_per_day"]))
        cfg["rsi_oversold"]       = float(data.get("rsi_oversold", cfg["rsi_oversold"]))
        cfg["vwap_entry_buffer"]  = float(data.get("vwap_entry_buffer", cfg["vwap_entry_buffer"]))
        cfg["risk_per_trade_pct"] = float(data.get("risk_per_trade_pct", cfg["risk_per_trade_pct"]))
        cfg["max_daily_loss"]     = float(data.get("max_daily_loss", cfg["max_daily_loss"]))
        cfg["max_open_positions"] = int(data.get("max_open_positions", cfg["max_open_positions"]))
        if "starting_capital" in data:
            cap = float(data["starting_capital"])
            cfg["starting_capital"] = cap
            cfg["current_capital"]  = cap
            paper_state["performance"]["starting_capital"] = cap
            paper_state["performance"]["current_capital"]  = cap
    add_paper_log("Paper config updated", "info")

# ─── RISK CHECKS ─────────────────────────────────────────────────────────────
def can_paper_trade(symbol: str = None) -> tuple[bool, str]:
    cfg = paper_state["config"]
    if paper_state["halted"]:
        return False, "Paper bot halted"
    if paper_state["daily_pnl"] <= -cfg["max_daily_loss"]:
        paper_state["halted"] = True
        return False, f"Paper daily loss limit ₹{cfg['max_daily_loss']} hit"
    if paper_state["trade_count"] >= cfg["max_trades_per_day"]:
        return False, "Paper max trades/day reached"
    if len(paper_state["open_positions"]) >= cfg["max_open_positions"]:
        return False, "Paper max open positions reached"
    if symbol:
        strikes = paper_state.get("strike_count", {})
        if strikes.get(symbol, 0) >= 2:
            return False, f"Paper two-strike rule: {symbol} banned"
    return True, ""

def _add_strike(symbol: str):
    sc = paper_state.setdefault("strike_count", {})
    sc[symbol] = sc.get(symbol, 0) + 1

# ─── PAPER ORDER ─────────────────────────────────────────────────────────────
def paper_place_order(symbol: str, side: str, qty: int,
                      price: float, sl: float = 0, tp: float = 0) -> str:
    """Simulate an order — instant fill at current price."""
    order_id = next_order_id()
    brokerage = paper_state["config"].get("brokerage_per_order", 20)
    paper_state["performance"]["brokerage_paid"] += brokerage

    record = {
        "timestamp":   datetime.now().isoformat(),
        "order_id":    order_id,
        "symbol":      symbol,
        "side":        side,
        "qty":         qty,
        "price":       round(price, 2),
        "sl":          round(sl, 2),
        "tp":          round(tp, 2),
        "brokerage":   brokerage,
        "market_bias": paper_state.get("market_bias", ""),
        "type":        "PAPER",
    }
    paper_state["trade_history"].append(record)
    add_paper_log(
        f"ORDER {side} {qty}x {symbol} @ {fmt(price)} "
        f"SL:{fmt(sl)} TP:{fmt(tp)} [{order_id}]",
        "buy" if side == "BUY" else "sell"
    )
    return order_id

# ─── STRATEGY RUNNER ─────────────────────────────────────────────────────────
def run_paper_tick(watchlist: list, indicator_cache: dict,
                   market_bias: str, ltp_cache: dict):
    """
    Called every strategy tick. Mirrors hybrid strategy logic
    but operates on paper_state with no real orders.
    """
    if not paper_state["enabled"]:
        return

    today = datetime.today().strftime("%Y-%m-%d")
    if paper_state["session_date"] != today:
        _reset_daily_state()
        paper_state["session_date"] = today
        add_paper_log("New paper session — daily state reset", "info")

    paper_state["market_bias"] = market_bias
    cfg = paper_state["config"]

    # ── Update MTM ───────────────────────────────────────────────────────────
    total_mtm = 0.0
    for sym, pos in paper_state["open_positions"].items():
        ltp   = ltp_cache.get(sym, pos["entry_price"])
        entry = pos["entry_price"]
        qty   = pos["qty"]
        total_mtm += (ltp - entry) * qty
    paper_state["mtm_pnl"] = round(total_mtm, 2)

    mode = cfg.get("mode", "hybrid")
    if mode in ("hybrid", "margin"):
        _paper_hybrid(watchlist, indicator_cache, ltp_cache, cfg)

def _paper_hybrid(watchlist, indicator_cache, ltp_cache, cfg):
    bias = paper_state["market_bias"]

    # ── Check exits for ALL open positions regardless of bias ─────────────────
    for symbol in list(paper_state["open_positions"].keys()):
        ind   = indicator_cache.get(symbol, {})
        ltp   = float(ltp_cache.get(symbol, 0))
        if ltp <= 0:
            continue
        ema9  = float(ind.get("ema9", 0))
        rsi   = float(ind.get("rsi", 50))
        atr   = float(ind.get("atr", 0))
        pos   = paper_state["open_positions"][symbol]
        entry = pos["entry_price"]
        qty   = pos["qty"]
        pnl_pct = (ltp - entry) / entry * 100

        new_trail = calculate_trailing_sl(entry, ltp, atr, "BUY")
        if new_trail > pos["trail_sl"]:
            pos["trail_sl"] = new_trail

        should_exit = False
        exit_reason = ""

        if pnl_pct >= cfg["take_profit_pct"]:
            should_exit = True
            exit_reason = f"TAKE PROFIT +{pnl_pct:.2f}%"
        elif ltp <= pos["trail_sl"]:
            should_exit = True
            exit_reason = f"TRAIL SL @ ₹{pos['trail_sl']:,.2f}"
        elif pnl_pct <= -cfg["stop_loss_pct"]:
            should_exit = True
            exit_reason = f"STOP LOSS {pnl_pct:.2f}%"
        elif rsi >= 78:
            should_exit = True
            exit_reason = f"RSI overbought ({rsi:.1f})"
        elif ltp < ema9 and pnl_pct > 0:
            should_exit = True
            exit_reason = "EMA9 breakdown"

        if should_exit:
            _paper_exit(symbol, qty, ltp, entry, exit_reason)

    # ── Only enter new positions in BULL bias ─────────────────────────────────
    if bias not in ("BULL", "STRONG_BULL"):
        return

    for item in sorted(watchlist, key=lambda x: x.get("priority", 9)):
        symbol = item["symbol"]
        if symbol in paper_state["open_positions"]:
            continue  # Already handled exit above

        ind  = indicator_cache.get(symbol, {})
        if not ind:
            continue

        ltp  = float(ltp_cache.get(symbol, 0))
        if ltp <= 0:
            continue

        vwap = float(ind.get("vwap", 0))
        rsi  = float(ind.get("rsi", 50))
        atr  = float(ind.get("atr", 0))
        boll = ind.get("bollinger", {})

        ok, reason = can_paper_trade(symbol)
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
                    capital     = cfg.get("current_capital", 200000),
                    risk_pct    = cfg["risk_per_trade_pct"],
                    entry_price = ltp,
                    sl_pct      = cfg["stop_loss_pct"],
                    min_qty     = item.get("qty", 1),
                )
            else:
                qty = item.get("qty", 1)

            sl_price = round(ltp * (1 - cfg["stop_loss_pct"] / 100), 2)
            tp_price = round(ltp * (1 + cfg["take_profit_pct"] / 100), 2)

            add_paper_log(
                f"ENTRY {symbol} LTP:₹{ltp:,.2f} "
                f"VWAP:₹{vwap:,.2f} RSI:{rsi:.1f} Qty:{qty}", "buy"
            )
            oid = paper_place_order(symbol, "BUY", qty,
                                    ltp, sl_price, tp_price)
            paper_state["open_positions"][symbol] = {
                "entry_price": ltp,
                "qty":         qty,
                "side":        "BUY",
                "order_id":    oid,
                "sl_price":    sl_price,
                "tp_price":    tp_price,
                "trail_sl":    sl_price,
                "entry_time":  datetime.now().isoformat(),
                "entry_vwap":  vwap,
                "entry_rsi":   rsi,
                "atr":         atr,
            }
            paper_state["trade_count"]               += 1
            paper_state["performance"]["total_trades"] += 1

def _paper_exit(symbol: str, qty: int, ltp: float,
                entry: float, reason: str = ""):
    pnl    = round((ltp - entry) * qty, 2)
    is_win = pnl > 0
    brok   = paper_state["config"].get("brokerage_per_order", 20)

    paper_state["daily_pnl"] += pnl
    paper_state["trade_count"] += 1
    perf = paper_state["performance"]
    perf["gross_pnl"]   += pnl
    perf["total_trades"] += 1
    perf["current_capital"] = perf.get("starting_capital", 200000) + perf["gross_pnl"]
    paper_state["config"]["current_capital"] = perf["current_capital"]

    if is_win:
        perf["wins"]      += 1
        perf["best_trade"] = max(perf["best_trade"], pnl)
    else:
        perf["losses"]     += 1
        perf["worst_trade"] = min(perf["worst_trade"], pnl)
        _add_strike(symbol)

    paper_place_order(symbol, "SELL", qty, ltp)
    del paper_state["open_positions"][symbol]

    add_paper_log(
        f"CLOSED {symbol} | PnL:{fmt(pnl)} | Daily:{fmt(paper_state['daily_pnl'])} | {reason}",
        "buy" if is_win else "alert"
    )

def paper_square_off_all(ltp_cache: dict, reason: str = "Manual"):
    """Force close all paper positions."""
    add_paper_log(f"SQUARE OFF ALL — {reason}", "alert")
    for symbol, pos in list(paper_state["open_positions"].items()):
        ltp = ltp_cache.get(symbol, pos["entry_price"])
        _paper_exit(symbol, pos["qty"], ltp, pos["entry_price"], reason)
    add_paper_log("All paper positions closed.", "alert")

# ─── SAFE STATE FOR API ──────────────────────────────────────────────────────
def get_safe_state(ltp_cache: dict, indicator_cache: dict) -> dict:
    """Return JSON-serializable paper state for the dashboard."""
    positions = []
    for sym, pos in paper_state["open_positions"].items():
        ltp   = float(ltp_cache.get(sym, pos["entry_price"]))
        entry = float(pos["entry_price"])
        qty   = int(pos["qty"])
        ind   = indicator_cache.get(sym, {})
        pnl   = round((ltp - entry) * qty, 2)
        positions.append({
            "sym":   sym,
            "type":  "EQUITY",
            "qty":   qty,
            "entry": round(entry, 2),
            "ltp":   round(ltp, 2),
            "pnl":   pnl,
            "side":  str(pos.get("side", "BUY")),
            "sl":    round(float(pos.get("trail_sl", pos.get("sl_price", 0)) or 0), 2),
            "tp":    round(float(pos.get("tp_price", 0) or 0), 2),
            "rsi":   round(float(ind.get("rsi", 0) or 0), 2),
            "vwap":  round(float(ind.get("vwap", 0) or 0), 2),
            "bias":  str(ind.get("bias", "")),
        })

    perf = paper_state["performance"]
    total = int(perf.get("total_trades", 0))
    wins  = int(perf.get("wins", 0))
    gross = float(perf.get("gross_pnl", 0))
    brok  = float(perf.get("brokerage_paid", 0))
    cfg   = paper_state["config"]

    return {
        "enabled":          bool(paper_state["enabled"]),
        "halted":           bool(paper_state["halted"]),
        "daily_pnl":        round(float(paper_state["daily_pnl"]), 2),
        "mtm_pnl":          round(float(paper_state["mtm_pnl"]), 2),
        "trade_count":      int(paper_state["trade_count"]),
        "market_bias":      str(paper_state["market_bias"]),
        "open_positions":   positions,
        "logs":             list(paper_state["activity_log"])[-60:],
        "strike_count":     {k: int(v) for k, v in paper_state.get("strike_count", {}).items()},
        "performance": {
            "total_trades":    total,
            "wins":            wins,
            "losses":          int(perf.get("losses", 0)),
            "gross_pnl":       round(gross, 2),
            "brokerage_paid":  round(brok, 2),
            "net_pnl":         round(gross - brok, 2),
            "best_trade":      round(float(perf.get("best_trade", 0)), 2),
            "worst_trade":     round(float(perf.get("worst_trade", 0)), 2),
            "win_rate":        round(wins / total * 100, 1) if total > 0 else 0.0,
            "avg_win":         round(gross / wins, 2) if wins > 0 else 0.0,
            "starting_capital": round(float(perf.get("starting_capital", 200000)), 2),
            "current_capital":  round(float(perf.get("current_capital", 200000)), 2),
            "return_pct":       round((perf.get("current_capital", 200000) -
                                       perf.get("starting_capital", 200000)) /
                                      perf.get("starting_capital", 200000) * 100, 2)
                                if perf.get("starting_capital", 0) > 0 else 0.0,
        },
        "config": {
            "mode":               cfg.get("mode", "hybrid"),
            "take_profit_pct":    cfg.get("take_profit_pct", 1.5),
            "stop_loss_pct":      cfg.get("stop_loss_pct", 0.35),
            "max_trades_per_day": cfg.get("max_trades_per_day", 6),
            "rsi_oversold":       cfg.get("rsi_oversold", 45),
            "vwap_entry_buffer":  cfg.get("vwap_entry_buffer", 0.0015),
            "risk_per_trade_pct": cfg.get("risk_per_trade_pct", 0.5),
            "max_daily_loss":     cfg.get("max_daily_loss", 3000),
            "max_open_positions": cfg.get("max_open_positions", 3),
            "starting_capital":   cfg.get("starting_capital", 200000),
            "current_capital":    cfg.get("current_capital", 200000),
        },
        "trade_history": list(reversed(paper_state["trade_history"][-100:])),
    }
