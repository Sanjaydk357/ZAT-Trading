# ZeroBot — Zerodha Automated Trading Bot

Automated buy/sell bot using the official `pykiteconnect` library with:
- **Margin strategy** — buys on dips, exits on profit/stop-loss
- **Arbitrage strategy** — cash-futures spread trading
- **Risk management** — daily loss limit, position caps, trade count limits
- **Live dashboard** — `index.html` for monitoring

---

## Quick Start

### 1. Install dependencies
```bash
pip install kiteconnect schedule
```

### 2. Get your Kite API credentials
1. Log in at https://developers.kite.trade
2. Create an app → note `api_key` and `api_secret`
3. Set redirect URL to `http://127.0.0.1`

### 3. Configure `trader.py`
Edit the `CONFIG` block at the top:
```python
CONFIG = {
    "api_key":    "YOUR_API_KEY",
    "api_secret": "YOUR_API_SECRET",
    ...
    "strategy": {
        "mode":            "margin",  # margin | arbitrage | both
        "margin_pct":      0.5,       # buy when price drops 0.5% from open
        "take_profit_pct": 1.0,       # sell at +1% profit
        "stop_loss_pct":   0.5,       # sell at -0.5% loss
        "max_trades_per_day": 10,
    },
    "watchlist": [
        {"symbol": "RELIANCE", "exchange": "NSE", "qty": 1},
        ...
    ],
    "risk": {
        "max_daily_loss":   5000,  # ₹ — halt bot if exceeded
        "max_open_positions": 5,
        "capital_per_trade": 50000,
    },
}
```

### 4. Run
```bash
python trader.py
```

On first run you'll be prompted to paste a `request_token`. The token is saved to `access_token.json` and auto-loaded next time.

---

## Strategy Details

### Margin / Dip-Buy Strategy
| Step | Logic |
|------|-------|
| Reference | Captures price at session open |
| Entry | Buys when LTP drops `margin_pct`% below reference |
| Exit (TP) | Sells when profit reaches `take_profit_pct`% |
| Exit (SL) | Sells when loss hits `stop_loss_pct`% |
| EOD | Auto squares off at 15:20 |

### Arbitrage Strategy
| Step | Logic |
|------|-------|
| Monitor | Watches futures vs spot spread |
| Entry | BUY spot + SELL futures when spread > threshold |
| Exit | SELL spot + BUY futures when spread narrows |

---

## Risk Controls

| Guard | Default | Description |
|-------|---------|-------------|
| Daily loss limit | ₹5,000 | Bot halts entirely |
| Max open positions | 5 | No new entries |
| Max trades / day | 10 | Prevents overtrading |
| EOD square-off | 15:20 | No overnight carry |
| Stop-loss | 0.5% | Per position hard exit |

---

## Files

| File | Description |
|------|-------------|
| `trader.py` | Core bot — run this |
| `dashboard.html` | Open in browser for live monitoring |
| `trading_bot.log` | Rotating log file |
| `trades.json` | JSONL trade history |
| `access_token.json` | Cached daily token |

---

## ⚠️ Disclaimer
This software is for **educational purposes**. Live trading with real capital carries significant risk. Always test with paper trading first. The authors are not responsible for any financial losses.
