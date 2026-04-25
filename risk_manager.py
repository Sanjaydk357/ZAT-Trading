"""
Advanced Risk Management Module.
Handles all risk checks, position sizing, and kill switches.
"""
import pytz
import logging
from datetime import datetime, time as dtime

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class RiskManager:
    """
    Centralized risk management for the trading bot.

    Rules:
    1. Max daily loss (hard stop)
    2. Max trades per day
    3. Max open positions
    4. Two-strike rule per symbol
    5. No trading in first 15 minutes (9:15-9:30)
    6. No new trades after 2:00 PM
    7. Capital per trade limit
    """

    def __init__(self, config: dict, state: dict):
        self.config = config
        self.state  = state
        self.log    = logging.getLogger(self.__class__.__name__)

    # ── Main Check ────────────────────────────────────────────────────────────
    def can_trade(self, symbol: str = None) -> tuple[bool, str]:
        cfg = self.config
        if self.state["halted"]:
            return False, "Bot halted"
        if self.state.get("market_stop"):
            return False, "Market circuit breaker triggered"
    
        # Use IST time
        now = datetime.now(IST).time()
    
        no_fly_end = dtime(9, 30)
        mkt_open   = cfg["market_open"]
        if mkt_open <= now < no_fly_end:
            return False, "No-fly zone (9:15-9:30 AM)"
    
        cutoff_h = cfg["strategy"].get("trade_cutoff_hour", 14)
        cutoff_m = cfg["strategy"].get("trade_cutoff_minute", 0)
        if now >= dtime(cutoff_h, cutoff_m):
            return False, f"No new trades after {cutoff_h:02d}:{cutoff_m:02d}"
    
        risk     = cfg["risk"]
        daily_pnl = self.state["daily_pnl"]
        if daily_pnl <= -risk["max_daily_loss"]:
            self.state["halted"] = True
            self.state["market_stop"] = True
            return False, f"Daily loss limit ₹{risk['max_daily_loss']} hit"
    
        if self.state["trade_count"] >= cfg["strategy"]["max_trades_per_day"]:
            return False, "Max trades per day reached"
    
        if len(self.state["open_positions"]) >= risk["max_open_positions"]:
            return False, "Max open positions reached"
    
        if symbol:
            strikes = self.state.get("strike_count", {})
            if strikes.get(symbol, 0) >= 2:
                return False, f"Two-strike rule: {symbol} banned today"
    
        return True, ""
    
    
        # ── Record Strike ─────────────────────────────────────────────────────────
        def add_strike(self, symbol: str):
            """Add a loss strike for a symbol (Two-Strike Rule)."""
            if "strike_count" not in self.state:
                self.state["strike_count"] = {}
            self.state["strike_count"][symbol] = \
                self.state["strike_count"].get(symbol, 0) + 1
            strikes = self.state["strike_count"][symbol]
            self.log.info(f"[STRIKE] {symbol}: {strikes}/2")
            return strikes

    # ── Daily Reset ───────────────────────────────────────────────────────────
    def daily_reset(self):
        """Reset all daily counters at start of new session."""
        self.state["daily_pnl"]    = 0.0
        self.state["trade_count"]  = 0
        self.state["halted"]       = False
        self.state["market_stop"]  = False
        self.state["strike_count"] = {}
        self.state["open_positions"] = {}
        self.state["reference_prices"] = {}
        self.log.info("[RISK] Daily state reset complete.")

    # ── MTM Check ─────────────────────────────────────────────────────────────
    def check_mtm(self, ltp_cache: dict) -> float:
        """
        Calculate Mark-to-Market P&L across all open positions.
        If MTM hits -1% of capital, trigger circuit breaker.
        """
        total_mtm = 0.0
        capital   = self.config["risk"].get("total_capital", 200000)

        for sym, pos in self.state["open_positions"].items():
            if sym.startswith("ARB_"):
                continue
            ltp   = ltp_cache.get(sym, pos["entry_price"])
            entry = pos["entry_price"]
            qty   = pos["qty"]
            mtm   = (ltp - entry) * qty
            total_mtm += mtm

        # Circuit breaker at -1% of capital
        if total_mtm <= -(capital * 0.01):
            self.state["market_stop"] = True
            self.log.warning(f"[CIRCUIT BREAKER] MTM Loss: ₹{total_mtm:.2f}")

        self.state["mtm_pnl"] = round(total_mtm, 2)
        return total_mtm
