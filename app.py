"""
app.py — Flask Backend + ZeroBot Pro Entry Point
Connects trader_engine + paper_trader to the dashboard.
"""

import os
import json
import logging
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect

import paper_trader as paper

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("zerobot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")

# ─── Import engine AFTER logging is set up ────────────────────────────────────
import trader_engine as engine

# ═══════════════════════════════════════════════════════════════════════════════
#  PAGE ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    login_needed = not engine.is_authenticated()
    login_url    = engine.kite.login_url() if login_needed else ""
    return render_template("index.html",
                           login_needed=login_needed,
                           login_url=login_url)


@app.route("/auth", methods=["POST"])
def auth():
    token = request.form.get("request_token", "").strip()
    if not token:
        return redirect("/")
    try:
        engine.authenticate(token)
    except Exception as e:
        log.error(f"[AUTH] {e}")
        engine.add_log(f"Auth failed: {e}", "alert")
    return redirect("/")


# ═══════════════════════════════════════════════════════════════════════════════
#  LIVE STATE API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/state")
def api_state():
    """Main dashboard state — everything the live tab needs."""
    s    = engine.state
    cfg  = engine.CONFIG["strategy"]
    risk = engine.CONFIG["risk"]

    # ── Watchlist rows ────────────────────────────────────────────────────────
    watchlist_rows = []
    for item in engine.CONFIG["watchlist"]:
        sym = item["symbol"]
        ex  = item["exchange"]
        ind = s["indicator_cache"].get(sym, {})
        ltp = float(s["ltp_cache"].get(sym, 0.0))
        vwap = float(ind.get("vwap", 0) or 0)
        chg  = round(((ltp - vwap) / vwap * 100), 2) if vwap > 0 else 0.0
        watchlist_rows.append({
            "sym":   sym,
            "ex":    ex,
            "ltp":   round(ltp, 2),
            "vwap":  round(vwap, 2),
            "ema9":  round(float(ind.get("ema9",  0) or 0), 2),
            "ema20": round(float(ind.get("ema20", 0) or 0), 2),
            "rsi":   round(float(ind.get("rsi",  50) or 50), 2),
            "atr":   round(float(ind.get("atr",   0) or 0), 2),
            "bias":  str(ind.get("bias", "NEUTRAL")),
            "chg":   chg,
        })

    # ── Open positions ────────────────────────────────────────────────────────
    positions = []
    for sym, pos in s["open_positions"].items():
        if sym.startswith("ARB_"):
            positions.append({
                "sym":   sym,
                "type":  "ARB",
                "qty":   int(pos.get("qty", 0)),
                "entry": 0.0,
                "ltp":   0.0,
                "pnl":   0.0,
                "side":  "ARB",
                "sl":    0.0,
                "tp":    0.0,
            })
        else:
            ltp   = float(s["ltp_cache"].get(sym, pos["entry_price"]))
            entry = float(pos["entry_price"])
            qty   = int(pos["qty"])
            positions.append({
                "sym":   sym,
                "type":  "EQUITY",
                "qty":   qty,
                "entry": round(entry, 2),
                "ltp":   round(ltp, 2),
                "pnl":   round((ltp - entry) * qty, 2),
                "side":  str(pos.get("side", "BUY")),
                "sl":    round(float(pos.get("trail_sl",
                               pos.get("sl_price", 0)) or 0), 2),
                "tp":    round(float(pos.get("tp_price", 0) or 0), 2),
            })

    # ── Performance ───────────────────────────────────────────────────────────
    perf  = s["performance"]
    total = int(perf.get("total_trades", 0))
    wins  = int(perf.get("wins", 0))
    gross = float(perf.get("gross_pnl", 0))
    brok  = float(perf.get("brokerage_paid", 0))

    return jsonify({
        "authenticated":  engine.is_authenticated(),
        "bot_running":    bool(engine.bot_running),
        "halted":         bool(s["halted"]),
        "market_stop":    bool(s.get("market_stop", False)),
        "daily_pnl":      round(float(s["daily_pnl"]), 2),
        "mtm_pnl":        round(float(s.get("mtm_pnl", 0)), 2),
        "trade_count":    int(s["trade_count"]),
        "market_bias":    str(s["market_bias"]),
        "open_positions": positions,
        "watchlist":      watchlist_rows,
        "indicators":     _safe_indicators(s["indicator_cache"]),
        "logs":           list(s["activity_log"])[-80:],
        "strike_count":   {k: int(v) for k, v
                          in s.get("strike_count", {}).items()},
        "profile":        dict(s.get("profile", {})),
        "funds":          dict(s.get("funds", {})),
        "config": {
            "mode":               cfg.get("mode", "hybrid"),
            "take_profit_pct":    cfg.get("take_profit_pct", 1.5),
            "stop_loss_pct":      cfg.get("stop_loss_pct", 0.35),
            "risk_per_trade_pct": cfg.get("risk_per_trade_pct", 0.5),
            "vwap_entry_buffer":  cfg.get("vwap_entry_buffer", 0.0015),
            "rsi_oversold":       cfg.get("rsi_oversold", 45),
            "max_trades_per_day": cfg.get("max_trades_per_day", 6),
        },
        "risk": {
            "max_daily_loss":     risk.get("max_daily_loss", 3000),
            "max_open_positions": risk.get("max_open_positions", 3),
            "total_capital":      risk.get("total_capital", 200000),
        },
        "performance": {
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
        },
    })


def _safe_indicators(cache: dict) -> dict:
    """Convert indicator cache to JSON-safe dict."""
    result = {}
    for sym, ind in cache.items():
        if not isinstance(ind, dict):
            continue
        macd = ind.get("macd", {}) or {}
        boll = ind.get("bollinger", {}) or {}
        result[sym] = {
            "ltp":    round(float(ind.get("ltp",    0) or 0), 2),
            "vwap":   round(float(ind.get("vwap",   0) or 0), 2),
            "ema9":   round(float(ind.get("ema9",   0) or 0), 2),
            "ema20":  round(float(ind.get("ema20",  0) or 0), 2),
            "ema200": round(float(ind.get("ema200", 0) or 0), 2),
            "rsi":    round(float(ind.get("rsi",   50) or 50), 2),
            "atr":    round(float(ind.get("atr",    0) or 0), 2),
            "bias":   str(ind.get("bias", "NEUTRAL")),
            "macd": {
                "macd":      round(float(macd.get("macd",      0) or 0), 4),
                "signal":    round(float(macd.get("signal",    0) or 0), 4),
                "histogram": round(float(macd.get("histogram", 0) or 0), 4),
            },
            "bollinger": {
                "upper":  round(float(boll.get("upper",  0) or 0), 2),
                "middle": round(float(boll.get("middle", 0) or 0), 2),
                "lower":  round(float(boll.get("lower",  0) or 0), 2),
                "width":  round(float(boll.get("width",  0) or 0), 2),
            },
        }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  BOT CONTROL ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/start", methods=["POST"])
def api_start():
    if not engine.is_authenticated():
        return jsonify({"ok": False,
                        "msg": "Not authenticated. Please login first."})
    if engine.bot_running:
        return jsonify({"ok": False, "msg": "Bot is already running."})
    if engine.state["halted"] or engine.state.get("market_stop"):
        return jsonify({"ok": False,
                        "msg": "Bot is halted. Click Reset Halt first."})
    try:
        t = threading.Thread(target=engine.start_bot,
                             daemon=True, name="BotThread")
        t.start()
        engine.add_log("Bot started from dashboard", "info")
        return jsonify({"ok": True, "msg": "Bot started successfully."})
    except Exception as e:
        log.error(f"[START] {e}")
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    try:
        engine.stop_bot()
        return jsonify({"ok": True, "msg": "Bot stopped."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/square_off", methods=["POST"])
def api_square_off():
    if not engine.is_authenticated():
        return jsonify({"ok": False, "msg": "Not authenticated."})
    try:
        engine.square_off_all("Manual — dashboard")
        return jsonify({"ok": True, "msg": "All live positions squared off."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/reset_halt", methods=["POST"])
def api_reset_halt():
    engine.state["halted"]      = False
    engine.state["market_stop"] = False
    engine.add_log("Halt reset by user", "alert")
    return jsonify({"ok": True, "msg": "Halt cleared."})


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(force=True) or {}
    try:
        cfg  = engine.CONFIG["strategy"]
        risk = engine.CONFIG["risk"]
        _apply = {
            "mode":               (str,   cfg,  "mode"),
            "take_profit_pct":    (float, cfg,  "take_profit_pct"),
            "stop_loss_pct":      (float, cfg,  "stop_loss_pct"),
            "risk_per_trade_pct": (float, cfg,  "risk_per_trade_pct"),
            "vwap_entry_buffer":  (float, cfg,  "vwap_entry_buffer"),
            "rsi_oversold":       (float, cfg,  "rsi_oversold"),
            "max_trades_per_day": (int,   cfg,  "max_trades_per_day"),
            "max_daily_loss":     (float, risk, "max_daily_loss"),
            "max_open_positions": (int,   risk, "max_open_positions"),
        }
        for key, (typ, target, field) in _apply.items():
            if key in data:
                target[field] = typ(data[key])

        engine.add_log("Live config updated from dashboard", "info")
        return jsonify({"ok": True, "msg": "Config updated."})
    except Exception as e:
        log.error(f"[CONFIG] {e}")
        return jsonify({"ok": False, "msg": str(e)})


# ═══════════════════════════════════════════════════════════════════════════════
#  LIVE TRADE HISTORY + PERFORMANCE
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/trades")
def api_trades():
    try:
        trades = []
        path   = engine.CONFIG.get("trade_log", "trades.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except Exception:
                            pass
        return jsonify({"trades": list(reversed(trades[-100:]))})
    except Exception as e:
        log.error(f"[TRADES] {e}")
        return jsonify({"trades": []})


@app.route("/api/performance")
def api_performance():
    perf  = engine.state["performance"]
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


# ═══════════════════════════════════════════════════════════════════════════════
#  PAPER TRADING ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/paper/state")
def api_paper_state():
    ltp_cache = engine.state["ltp_cache"]
    ind_cache = engine.state["indicator_cache"]
    return jsonify(paper.get_safe_state(ltp_cache, ind_cache))


@app.route("/api/paper/enable", methods=["POST"])
def api_paper_enable():
    data = request.get_json(force=True) or {}
    try:
        override = {}
        if "starting_capital" in data:
            cap = float(data["starting_capital"])
            override["starting_capital"] = cap
            override["current_capital"]  = cap
        paper.enable_paper(override)
        return jsonify({"ok": True, "msg": "Paper trading enabled."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/paper/disable", methods=["POST"])
def api_paper_disable():
    try:
        paper.disable_paper()
        return jsonify({"ok": True, "msg": "Paper trading disabled."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    data = request.get_json(force=True) or {}
    try:
        if "starting_capital" in data:
            cap = float(data["starting_capital"])
            paper.paper_state["config"]["starting_capital"] = cap
        paper.reset_paper()
        return jsonify({"ok": True, "msg": "Paper state reset."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/search_symbol")
def search_symbol():
    """
    Search for exact symbol name on NSE.
    Usage: /api/search_symbol?q=HDFC
    """
    query    = request.args.get("q", "").upper()
    exchange = request.args.get("ex", "NSE")
    if not engine.is_authenticated():
        return jsonify({"error": "Not authenticated"})
    try:
        instruments = engine.kite.instruments(exchange)
        matches = [
            {
                "symbol":  i["tradingsymbol"],
                "name":    i["name"],
                "token":   i["instrument_token"],
                "type":    i["instrument_type"],
            }
            for i in instruments
            if query in i["tradingsymbol"] or query in i["name"].upper()
        ][:20]  # limit to 20 results
        return jsonify({"matches": matches, "total": len(matches)})
    except Exception as e:
        return jsonify({"error": str(e)})
        


@app.route("/api/paper/config", methods=["POST"])
def api_paper_config():
    data = request.get_json(force=True) or {}
    try:
        paper.update_config(data)
        return jsonify({"ok": True, "msg": "Paper config updated."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/paper/square_off", methods=["POST"])
def api_paper_square_off():
    try:
        paper.paper_square_off_all(engine.state["ltp_cache"],
                                   "Manual — dashboard")
        return jsonify({"ok": True,
                        "msg": "All paper positions squared off."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/paper/trades")
def api_paper_trades():
    trades = list(reversed(
        paper.paper_state.get("trade_history", [])[-100:]
    ))
    return jsonify({"trades": trades})


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGOUT
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/logout", methods=["POST"])
def api_logout():
    try:
        if engine.bot_running:
            engine.stop_bot()

        token_file = engine.CONFIG.get("token_file", "access_token.json")
        if os.path.exists(token_file):
            os.remove(token_file)

        engine._auth = False
        try:
            engine.kite.set_access_token(None)
        except Exception:
            pass

        engine.state["profile"] = {}
        engine.state["funds"]   = {}
        engine.add_log("User logged out", "alert")
        return jsonify({"ok": True, "msg": "Logged out."})
    except Exception as e:
        log.error(f"[LOGOUT] {e}")
        return jsonify({"ok": False, "msg": str(e)})


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND THREADS
# ═══════════════════════════════════════════════════════════════════════════════

def _data_refresh_loop():
    """
    Refresh LTP + indicators for all watchlist symbols every 15 seconds.
    Keeps dashboard live even between 60-second strategy ticks.
    """
    import time as _time
    while True:
        try:
            if engine.is_authenticated() and engine.is_market_open():
                # Refresh index
                idx = engine.CONFIG["index"]
                try:
                    engine.get_ltp(idx["symbol"], idx["exchange"])
                except Exception:
                    pass

                # Refresh each watchlist symbol
                for item in engine.CONFIG["watchlist"]:
                    try:
                        engine.get_ltp(item["symbol"], item["exchange"])
                        engine.compute_indicators(
                            item["symbol"], item["exchange"])
                    except Exception as ex:
                        log.debug(f"[REFRESH] {item['symbol']}: {ex}")

                # Update market bias + MTM
                try:
                    engine.update_market_regime()
                except Exception:
                    pass
                try:
                    engine.risk_mgr.check_mtm(engine.state["ltp_cache"])
                except Exception:
                    pass

        except Exception as e:
            log.error(f"[DATA REFRESH] {e}")

        _time.sleep(15)


def _paper_tick_loop():
    """
    Run paper strategy tick every 60 seconds.
    Uses the same LTP + indicator cache as the live engine.
    No extra Zerodha API calls — completely free.
    """
    import time as _time
    while True:
        try:
            if paper.paper_state["enabled"]:
                paper.run_paper_tick(
                    watchlist       = engine.CONFIG["watchlist"],
                    indicator_cache = engine.state["indicator_cache"],
                    market_bias     = engine.state["market_bias"],
                    ltp_cache       = engine.state["ltp_cache"],
                )
        except Exception as e:
            log.error(f"[PAPER TICK] {e}")
        _time.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP + MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def _startup():
    """Load saved token and launch background threads."""
    # Try to restore previous session
    engine.try_load_token()

    # Data refresh thread — keeps LTP/indicators fresh every 15s
    threading.Thread(
        target=_data_refresh_loop,
        daemon=True,
        name="DataRefresh"
    ).start()
    log.info("[STARTUP] Data refresh thread started.")

    # Paper tick thread — runs paper strategy every 60s
    threading.Thread(
        target=_paper_tick_loop,
        daemon=True,
        name="PaperTick"
    ).start()
    log.info("[STARTUP] Paper tick thread started.")


if __name__ == "__main__":
    _startup()
    log.info("[STARTUP] ZeroBot Pro → http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
