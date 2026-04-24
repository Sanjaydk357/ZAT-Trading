"""
Flask Dashboard — Robust VWAP-EMA Hybrid Bot
Production version for Render.com deployment
"""

import os
import sys
import json
import logging
import threading
from datetime import datetime, time as dtime

from dotenv import load_dotenv
from flask import (Flask, render_template, jsonify,
                   request, redirect, url_for)

import paper_trader as paper
import trader_engine as engine

# ─── Load .env (local dev only) ───────────────────────────────────────────────
# On Render, env vars are set via dashboard — load_dotenv() is harmless there
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Use environment variable for secret key in production
app.secret_key = os.getenv("FLASK_SECRET_KEY", "zerobot_robust_2026_fallback")

# ─── Logging ──────────────────────────────────────────────────────────────────
# On Render — only use StreamHandler (no file logging, use Render log viewer)
IS_RENDER = os.getenv("RENDER", False)

handlers = [logging.StreamHandler(sys.stdout)]
if not IS_RENDER:
    # Local: also log to file
    handlers.append(
        logging.FileHandler("trading_bot.log", encoding="utf-8")
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=handlers
)
log = logging.getLogger(__name__)
log.info(f"[STARTUP] Running on Render: {bool(IS_RENDER)}")


# ─── Helper Functions ─────────────────────────────────────────────────────────
def make_json_safe(obj):
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_safe(i) for i in obj]
    elif isinstance(obj, dtime):
        return obj.strftime("%H:%M")
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif hasattr(obj, '__class__') and obj.__class__.__name__ == 'date':
        return str(obj)
    else:
        return obj


def safe_config():
    cfg = engine.CONFIG["strategy"]
    return {
        "mode":               cfg.get("mode", "hybrid"),
        "take_profit_pct":    cfg.get("take_profit_pct", 1.5),
        "stop_loss_pct":      cfg.get("stop_loss_pct", 0.35),
        "max_trades_per_day": cfg.get("max_trades_per_day", 6),
        "order_quantity":     cfg.get("order_quantity", 1),
        "ema_fast":           cfg.get("ema_fast", 9),
        "ema_slow":           cfg.get("ema_slow", 20),
        "ema_trend":          cfg.get("ema_trend", 200),
        "rsi_period":         cfg.get("rsi_period", 14),
        "rsi_oversold":       cfg.get("rsi_oversold", 45),
        "rsi_overbought":     cfg.get("rsi_overbought", 70),
        "atr_period":         cfg.get("atr_period", 14),
        "vwap_entry_buffer":  cfg.get("vwap_entry_buffer", 0.0015),
        "use_dynamic_sizing": cfg.get("use_dynamic_sizing", True),
        "risk_per_trade_pct": cfg.get("risk_per_trade_pct", 0.5),
        "trailing_sl":        cfg.get("trailing_sl", True),
        "margin_pct":         cfg.get("margin_pct", 0.5),
        "trade_cutoff":       (
            cfg["trade_cutoff"].strftime("%H:%M")
            if isinstance(cfg.get("trade_cutoff"), dtime)
            else str(cfg.get("trade_cutoff", "14:00"))
        ),
        "candle_interval":    cfg.get("candle_interval", "5minute"),
        "candle_lookback":    cfg.get("candle_lookback", 100),
    }


def safe_risk():
    r = engine.CONFIG["risk"]
    return {
        "max_daily_loss":     r.get("max_daily_loss", 3000),
        "max_open_positions": r.get("max_open_positions", 3),
        "capital_per_trade":  r.get("capital_per_trade", 50000),
        "total_capital":      r.get("total_capital", 200000),
        "max_capital_deploy": r.get("max_capital_deploy", 0.6),
    }


def safe_indicators():
    raw = engine.state.get("indicator_cache", {})
    result = {}
    for sym, ind in raw.items():
        if not isinstance(ind, dict):
            continue
        result[sym] = {
            "ltp":    float(ind.get("ltp", 0) or 0),
            "vwap":   float(ind.get("vwap", 0) or 0),
            "ema9":   float(ind.get("ema9", 0) or 0),
            "ema20":  float(ind.get("ema20", 0) or 0),
            "ema200": float(ind.get("ema200", 0) or 0),
            "rsi":    float(ind.get("rsi", 50) or 50),
            "atr":    float(ind.get("atr", 0) or 0),
            "bias":   str(ind.get("bias", "NEUTRAL")),
            "macd": {
                "macd":      float((ind.get("macd") or {}).get("macd", 0) or 0),
                "signal":    float((ind.get("macd") or {}).get("signal", 0) or 0),
                "histogram": float((ind.get("macd") or {}).get("histogram", 0) or 0),
            },
            "bollinger": {
                "upper":  float((ind.get("bollinger") or {}).get("upper", 0) or 0),
                "middle": float((ind.get("bollinger") or {}).get("middle", 0) or 0),
                "lower":  float((ind.get("bollinger") or {}).get("lower", 0) or 0),
                "width":  float((ind.get("bollinger") or {}).get("width", 0) or 0),
            },
        }
    return result


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    login_needed = not engine.is_authenticated()
    login_url    = engine.kite.login_url() if login_needed else None
    return render_template("index.html",
                           login_needed=login_needed,
                           login_url=login_url)


@app.route("/health")
def health():
    """Health check endpoint — Render uses this to verify app is running."""
    return jsonify({
        "status":        "ok",
        "authenticated": engine.is_authenticated(),
        "bot_running":   engine.bot_running,
        "market_open":   engine.is_market_open(),
        "timestamp":     datetime.now().isoformat(),
    })


@app.route("/auth", methods=["POST"])
def auth():
    token = request.form.get("request_token", "").strip()
    if not token:
        return redirect(url_for("index"))
    try:
        engine.authenticate(token)
        log.info("[AUTH] Login successful.")
    except Exception as e:
        log.error(f"[AUTH] {e}")
    return redirect(url_for("index"))


@app.route("/api/state")
def api_state():
    s = engine.state

    # ── Positions ─────────────────────────────────────────────────────────
    positions = []
    for sym, pos in s["open_positions"].items():
        try:
            if sym.startswith("ARB_"):
                ltp_a = engine.get_ltp_cached(pos["leg_a"]["symbol"])
                ltp_b = engine.get_ltp_cached(pos["leg_b"]["symbol"])
                pnl   = ((pos.get("entry_price_b", 0) - ltp_b) +
                         (ltp_a - pos.get("entry_price_a", 0))) * pos["qty"]
                positions.append({
                    "sym":   sym,
                    "type":  "ARB",
                    "qty":   int(pos["qty"]),
                    "entry": round((pos.get("entry_price_a", 0) +
                                    pos.get("entry_price_b", 0)) / 2, 2),
                    "ltp":   round((ltp_a + ltp_b) / 2, 2),
                    "pnl":   round(float(pnl), 2),
                    "side":  "ARB",
                    "sl": 0.0, "tp": 0.0,
                    "rsi": 0.0, "vwap": 0.0, "bias": "",
                })
            else:
                ltp   = engine.get_ltp_cached(sym)
                ind   = s["indicator_cache"].get(sym, {})
                entry = float(pos.get("entry_price", 0))
                qty   = int(pos.get("qty", 1))
                pnl   = round((float(ltp) - entry) * qty, 2)
                positions.append({
                    "sym":   sym,
                    "type":  "EQUITY",
                    "qty":   qty,
                    "entry": round(entry, 2),
                    "ltp":   round(float(ltp), 2),
                    "pnl":   pnl,
                    "side":  str(pos.get("side", "BUY")),
                    "sl":    round(float(
                        pos.get("trail_sl",
                                pos.get("sl_price", 0)) or 0), 2),
                    "tp":    round(float(pos.get("tp_price", 0) or 0), 2),
                    "rsi":   round(float(ind.get("rsi", 0) or 0), 2),
                    "vwap":  round(float(ind.get("vwap", 0) or 0), 2),
                    "bias":  str(ind.get("bias", "")),
                })
        except Exception as e:
            log.error(f"[STATE] Position error {sym}: {e}")
            continue

    # ── Watchlist ─────────────────────────────────────────────────────────
    watchlist = []
    for item in engine.CONFIG["watchlist"]:
        try:
            sym  = item["symbol"]
            ltp  = float(engine.get_ltp_cached(sym) or 0)
            ind  = s["indicator_cache"].get(sym, {})
            ref  = float(s["reference_prices"].get(sym, ltp) or ltp)
            chg  = round((ltp - ref) / ref * 100, 2) if ref > 0 else 0.0
            watchlist.append({
                "sym":   sym,
                "ex":    item["exchange"],
                "qty":   item.get("qty", 1),
                "ltp":   round(ltp, 2),
                "ref":   round(ref, 2),
                "chg":   chg,
                "vwap":  round(float(ind.get("vwap", 0) or 0), 2),
                "ema20": round(float(ind.get("ema20", 0) or 0), 2),
                "rsi":   round(float(ind.get("rsi", 0) or 0), 2),
                "bias":  str(ind.get("bias", "—")),
            })
        except Exception as e:
            log.error(f"[STATE] Watchlist error {item.get('symbol')}: {e}")
            continue

    # ── Performance ───────────────────────────────────────────────────────
    perf_raw    = s.get("performance", {})
    performance = {
        "total_trades":   int(perf_raw.get("total_trades", 0)),
        "wins":           int(perf_raw.get("wins", 0)),
        "losses":         int(perf_raw.get("losses", 0)),
        "gross_pnl":      round(float(perf_raw.get("gross_pnl", 0)), 2),
        "brokerage_paid": round(float(perf_raw.get("brokerage_paid", 0)), 2),
        "best_trade":     round(float(perf_raw.get("best_trade", 0)), 2),
        "worst_trade":    round(float(perf_raw.get("worst_trade", 0)), 2),
    }

    # ── Funds ─────────────────────────────────────────────────────────────
    funds_raw = s.get("funds", {})
    funds = {
        "equity_available": float(funds_raw.get("equity_available", 0)),
        "equity_used":      float(funds_raw.get("equity_used", 0)),
        "equity_total":     float(funds_raw.get("equity_total", 0)),
        "opening_balance":  float(funds_raw.get("opening_balance", 0)),
    }

    # ── Profile ───────────────────────────────────────────────────────────
    prof_raw = s.get("profile", {})
    profile  = {
        "name":      str(prof_raw.get("name", "")),
        "email":     str(prof_raw.get("email", "")),
        "user_id":   str(prof_raw.get("user_id", "")),
        "broker":    str(prof_raw.get("broker", "ZERODHA")),
        "user_type": str(prof_raw.get("user_type", "")),
        "exchanges": list(prof_raw.get("exchanges", [])),
        "products":  list(prof_raw.get("products", [])),
    }

    try:
        return jsonify({
            "authenticated":  bool(engine.is_authenticated()),
            "halted":         bool(s.get("halted", False)),
            "market_stop":    bool(s.get("market_stop", False)),
            "market_bias":    str(s.get("market_bias", "NEUTRAL")),
            "daily_pnl":      round(float(s.get("daily_pnl", 0)), 2),
            "mtm_pnl":        round(float(s.get("mtm_pnl", 0)), 2),
            "trade_count":    int(s.get("trade_count", 0)),
            "open_positions": positions,
            "watchlist":      watchlist,
            "logs":           list(s.get("activity_log", []))[-60:],
            "config":         safe_config(),
            "risk":           safe_risk(),
            "market_open":    bool(engine.is_market_open()),
            "bot_running":    bool(engine.bot_running),
            "profile":        profile,
            "funds":          funds,
            "performance":    performance,
            "strike_count":   {
                k: int(v) for k, v in
                s.get("strike_count", {}).items()
            },
            "indicators":     safe_indicators(),
        })
    except Exception as e:
        log.error(f"[API STATE] Error: {e}")
        return jsonify({
            "authenticated": bool(engine.is_authenticated()),
            "halted":        bool(s.get("halted", False)),
            "daily_pnl":     0.0, "mtm_pnl": 0.0,
            "trade_count":   0,
            "open_positions": [], "watchlist": [], "logs": [],
            "config":        safe_config(),
            "risk":          safe_risk(),
            "market_open":   bool(engine.is_market_open()),
            "bot_running":   bool(engine.bot_running),
            "profile":       {}, "funds":       {},
            "performance":   {}, "strike_count": {},
            "indicators":    {}, "error":        str(e),
        })


# ─── Paper Trading Routes ─────────────────────────────────────────────────────
@app.route("/api/paper/state")
def api_paper_state():
    try:
        safe = paper.get_safe_state(
            engine.state["ltp_cache"],
            engine.state["indicator_cache"]
        )
        return jsonify(safe)
    except Exception as e:
        log.error(f"[PAPER STATE] {e}")
        return jsonify({"enabled": False, "error": str(e)})


@app.route("/api/paper/enable", methods=["POST"])
def api_paper_enable():
    data = request.get_json() or {}
    try:
        cfg = {}
        if "starting_capital" in data:
            cfg["starting_capital"] = float(data["starting_capital"])
            cfg["current_capital"]  = float(data["starting_capital"])
        paper.enable_paper(cfg)
        return jsonify({"ok": True, "msg": "Paper trading enabled"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


@app.route("/api/paper/disable", methods=["POST"])
def api_paper_disable():
    paper.disable_paper()
    return jsonify({"ok": True, "msg": "Paper trading disabled"})


@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    data = request.get_json() or {}
    if "starting_capital" in data:
        paper.paper_state["config"]["starting_capital"] = float(
            data["starting_capital"])
    paper.reset_paper()
    return jsonify({"ok": True, "msg": "Paper state reset"})


@app.route("/api/paper/square_off", methods=["POST"])
def api_paper_square_off():
    paper.paper_square_off_all(engine.state["ltp_cache"], "Manual")
    return jsonify({"ok": True, "msg": "Paper positions squared off"})


@app.route("/api/paper/config", methods=["POST"])
def api_paper_config():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "msg": "No data"}), 400
    try:
        paper.update_config(data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


@app.route("/api/paper/trades")
def api_paper_trades():
    trades = list(reversed(
        paper.paper_state.get("trade_history", [])[-200:]
    ))
    return jsonify({"trades": trades})


# ─── Live Trading Routes ──────────────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def api_start():
    if not engine.is_authenticated():
        return jsonify({"ok": False, "msg": "Not authenticated"}), 401
    if engine.bot_running:
        return jsonify({"ok": False, "msg": "Already running"})
    threading.Thread(target=engine.start_bot, daemon=True).start()
    return jsonify({"ok": True, "msg": "Bot started"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    engine.stop_bot()
    return jsonify({"ok": True, "msg": "Bot stopped"})


@app.route("/api/square_off", methods=["POST"])
def api_square_off():
    if not engine.is_authenticated():
        return jsonify({"ok": False, "msg": "Not authenticated"}), 401
    engine.square_off_all("Manual")
    return jsonify({"ok": True, "msg": "All positions squared off"})


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "msg": "No data received"}), 400
    try:
        s = engine.CONFIG["strategy"]
        r = engine.CONFIG["risk"]
        s["mode"]               = str(data.get("mode", "hybrid"))
        s["take_profit_pct"]    = float(data.get("take_profit_pct", 1.5))
        s["stop_loss_pct"]      = float(data.get("stop_loss_pct", 0.35))
        s["max_trades_per_day"] = int(data.get("max_trades_per_day", 6))
        s["rsi_oversold"]       = float(data.get("rsi_oversold", 45))
        s["vwap_entry_buffer"]  = float(data.get("vwap_entry_buffer", 0.0015))
        s["risk_per_trade_pct"] = float(data.get("risk_per_trade_pct", 0.5))
        r["max_daily_loss"]     = float(data.get("max_daily_loss", 3000))
        r["max_open_positions"] = int(data.get("max_open_positions", 3))
        engine.add_log("Config updated via dashboard", "info")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


@app.route("/api/reset_halt", methods=["POST"])
def api_reset_halt():
    engine.state["halted"]      = False
    engine.state["market_stop"] = False
    engine.add_log("Halt reset by user", "alert")
    return jsonify({"ok": True})


@app.route("/api/trades")
def api_trades():
    trades = []
    try:
        with open(engine.CONFIG["trade_log"], encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        pass
    return jsonify({"trades": list(reversed(trades[-200:]))})


@app.route("/api/performance")
def api_performance():
    perf  = engine.state.get("performance", {})
    total = int(perf.get("total_trades", 0))
    wins  = int(perf.get("wins", 0))
    gross = float(perf.get("gross_pnl", 0))
    brok  = float(perf.get("brokerage_paid", 0))
    return jsonify({
        "total_trades":   total,
        "wins":           wins,
        "losses":         int(perf.get("losses", 0)),
        "gross_pnl":      round(gross, 2),
        "brokerage_paid": round(brok, 2),
        "net_pnl":        round(gross - brok, 2),
        "best_trade":     round(float(perf.get("best_trade", 0)), 2),
        "worst_trade":    round(float(perf.get("worst_trade", 0)), 2),
        "win_rate":       round(wins / total * 100, 1) if total > 0 else 0.0,
        "avg_win":        round(gross / wins, 2) if wins > 0 else 0.0,
    })


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  ZeroBot Pro — VWAP-EMA Hybrid + Paper Trading")
    print("  Visit: http://localhost:5000")
    print("=" * 55 + "\n")
    engine.try_load_token()
    # Use PORT env var (Render sets this automatically)
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, use_reloader=False)