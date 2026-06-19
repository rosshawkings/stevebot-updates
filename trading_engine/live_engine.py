#!/usr/bin/env python3
"""
live_engine.py — Steve Bot Trading Engine
═══════════════════════════════════════════
E2/D2 System: D2(35) pool rotation on structural coins,
zone bounce entries, trailing stop exits.

Reads config from /home/ubuntu/stevebot/config.json
Supports PAPER mode (local balance tracking) and LIVE mode (real Bitget orders).

Invoked by the Steve Bot agent for every 15m bar cycle.
Also serves data for: /status, /positions, /pnl, /history commands.
"""

import os, sys, json, time, hashlib, hmac, base64, logging, math, csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict, OrderedDict
from typing import Optional, Dict, List, Tuple, Any
import urllib.request, urllib.error

# ═══════════════════════════════════════════════════════════════
# PATHS & CONFIG
# ═══════════════════════════════════════════════════════════════

SB_DIR = os.environ.get("SB_DIR", "/home/ubuntu/stevebot")
CONFIG_PATH = os.environ.get("SB_CONFIG", os.path.join(SB_DIR, "config.json"))
TRADES_DIR = os.path.join(SB_DIR, "trades")
PERF_DIR = os.path.join(SB_DIR, "performance")
LOGS_DIR = os.path.join(SB_DIR, "logs")

for d in [TRADES_DIR, PERF_DIR, LOGS_DIR]:
    os.makedirs(d, exist_ok=True)

# ── Load config ──
try:
    with open(CONFIG_PATH) as f:
        CONFIG = json.load(f)
except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
    print(f"[ERROR] Config load failed: {e}")
    print("[WARNING] Using default config (paper mode, $500 balance)")
    CONFIG = {
        "paper_trading": True,
        "paper_balance": 500.0,
        "max_positions": 6,
        "max_total_risk": 0.15,
        "zone_strength_min": 4,
        "min_rr": 2.0,
        "trail_activate_r": 1.2,
        "trail_width_r": 1.0,
        "time_exit_bars": 384,
        "cooldown_bars": 192,
        "correlation_drop_pct": -1.5,
        "leverage_default": 5,
        "risk_default": 0.05,
        "notional_cap_mult": 0.55,
        "max_monthly_trades": 15,
    }

PAPER_MODE = CONFIG.get("paper_trading", True)
PAPER_BALANCE = CONFIG.get("paper_balance", 500.0)
BITGET_API_KEY = CONFIG.get("bitget_api_key", "")
BITGET_API_SECRET = CONFIG.get("bitget_api_secret", "")
BITGET_PASSPHRASE = CONFIG.get("bitget_passphrase", "")
BITGET_BASE = CONFIG.get("base_url", "https://api.bitget.com")
FUTURES_SUFFIX = CONFIG.get("futures_suffix", "_UMCBL")
DEEPSEEK_API_KEY = CONFIG.get("deepseek_api_key", "")
USER_NAME = CONFIG.get("user_name", "Trader")
UPDATE_URL = CONFIG.get("update_url", "https://stevebot-updates.pages.dev/version.json")

# ── Trading params ──
MAX_POS = CONFIG.get("max_positions", 6)
MAX_TOTAL_RISK = CONFIG.get("max_total_risk", 0.15)
MAX_MONTHLY = CONFIG.get("max_monthly_trades", 15)
ZS_MIN = CONFIG.get("zone_strength_min", 4)
MIN_RR = CONFIG.get("min_rr", 2.0)
TRAIL_ACTIVATE_R = CONFIG.get("trail_activate_r", 1.2)
TRAIL_WIDTH_R = CONFIG.get("trail_width_r", 1.0)
TIME_EXIT_BARS = CONFIG.get("time_exit_bars", 384)
COOLDOWN_BARS = CONFIG.get("cooldown_bars", 192)
BTC_CORR_THRESHOLD = CONFIG.get("correlation_drop_pct", -1.5)
DEFAULT_LEV = CONFIG.get("leverage_default", 5)
HIGH_LEV = CONFIG.get("leverage_high", 8)
DEFAULT_RISK_PCT = CONFIG.get("risk_default", 0.05)
HIGH_RISK_PCT = CONFIG.get("risk_high", 0.10)
NOTIONAL_CAP_MULT = CONFIG.get("notional_cap_mult", 0.55)

# ── Cost constants (from backtest_constants.py) ──
ENTRY_FEE_BLENDED = 0.00032    # 0.032%
EXIT_FEE_BLENDED = 0.00048     # 0.048%
FUNDING_PER_8H = 0.0001        # 0.01%
FUNDING_SETTLEMENTS_UTC = [0, 8, 16]

TOP4 = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
CORRELATED = {"ETHUSDT", "SOLUSDT", "BNBUSDT"}

# ── Slippage constants (per-side fractions) ──
SLIPPAGE_TOP4 = 0.0005      # 0.05% per side (BTC, ETH, SOL, BNB)
SLIPPAGE_MID = 0.0010       # 0.10% per side (high-cap alts)
SLIPPAGE_SMALL = 0.0030     # 0.30% per side (low-cap/small alts)

def get_slippage(symbol: str) -> float:
    if symbol in TOP4:
        return SLIPPAGE_TOP4
    if symbol in {"XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT"}:
        return SLIPPAGE_MID
    return SLIPPAGE_SMALL

# ── Structural coin universe (51 coins from coin_structural_filter.py) ──
# Default universe; updated by dynamic pool manager at runtime
DEFAULT_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "UNIUSDT", "NEARUSDT", "AVAXUSDT", "BCHUSDT", "ICPUSDT", "ADAUSDT",
    "LINKUSDT", "IOTAUSDT", "EOSUSDT", "WAVESUSDT", "ALGOUSDT",
    "RUNEUSDT", "JUPUSDT", "SEIUSDT", "STXUSDT", "FTMUSDT", "TRXUSDT",
    "FILUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "TIAUSDT",
    "INJUSDT", "LDOUSDT", "RNDRUSDT", "GRTUSDT", "MANAUSDT", "SANDUSDT",
    "THETAUSDT", "EGLDUSDT", "VETUSDT", "HBARUSDT", "ALGOUSDT",
    "XTZUSDT", "ETCUSDT", "LTCUSDT", "DOTUSDT", "ATOMUSDT",
    "KSMUSDT", "ZILUSDT", "BATUSDT", "ENJUSDT", "COMPUSDT",
]
POOL_SIZE = 35  # D2(35) — top 35 by volume

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, f"sb_{datetime.now().strftime('%Y%m%d')}.log")),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("stevebot")

# ═══════════════════════════════════════════════════════════════
# IN-MEMORY STATE
# ═══════════════════════════════════════════════════════════════

class State:
    """Thread-safe-ish state container for the trading engine."""
    def __init__(self):
        self.balance = PAPER_BALANCE if PAPER_MODE else 0.0
        self.locked_margin = 0.0
        self.positions: Dict[str, Dict] = OrderedDict()
        self.trade_history: List[Dict] = []
        self.monthly_trades: int = 0
        self.win_streak: int = 0
        self.loss_streak: int = 0
        self.daily_pnl: float = 0.0
        self.last_bar_ts: Optional[int] = None
        self.regime_score: float = 0.0
        self.regime_label: str = "UNKNOWN"
        self.gate_open: bool = True
        self.gate_reason: str = ""
        self.cooldowns: Dict[str, int] = {}
        self.pool: List[str] = DEFAULT_UNIVERSE[:POOL_SIZE]
        self.daily_snapshots: List[Dict] = []
        self._load_state()

    def _state_path(self) -> str:
        return os.path.join(SB_DIR, "engine_state.json")

    def _save_state(self):
        try:
            data = {
                "balance": self.balance,
                "locked_margin": self.locked_margin,
                "positions": {k: {kk: vv for kk, vv in v.items() if kk != "trail_peak"}
                               for k, v in self.positions.items()},
                "monthly_trades": self.monthly_trades,
                "win_streak": self.win_streak,
                "loss_streak": self.loss_streak,
                "cooldowns": self.cooldowns,
                "last_saved": datetime.now(timezone.utc).isoformat(),
            }
            with open(self._state_path(), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error(f"State save failed: {e}")

    def _load_state(self):
        sp = self._state_path()
        if os.path.exists(sp):
            try:
                with open(sp) as f:
                    data = json.load(f)
                self.balance = data.get("balance", self.balance)
                self.monthly_trades = data.get("monthly_trades", 0)
                self.win_streak = data.get("win_streak", 0)
                self.loss_streak = data.get("loss_streak", 0)
                self.cooldowns = data.get("cooldowns", {})
            except Exception:
                pass

    @property
    def free_balance(self) -> float:
        return max(0.0, self.balance - self.locked_margin)

    @property
    def max_monthly_trades(self) -> int:
        base = MAX_MONTHLY
        adj = (self.win_streak * 3) - (self.loss_streak * 3)
        return min(30, max(5, base + adj))

    @property
    def pnl_pct(self) -> float:
        start = PAPER_BALANCE if PAPER_MODE else CONFIG.get("starting_balance", self.balance)
        if start <= 0:
            return 0.0
        return ((self.balance / start) - 1.0) * 100.0

state = State()

# ═══════════════════════════════════════════════════════════════
# BITGET API CLIENT
# ═══════════════════════════════════════════════════════════════

class BitgetClient:
    def __init__(self):
        self.api_key = BITGET_API_KEY
        self.api_secret = BITGET_API_SECRET
        self.passphrase = BITGET_PASSPHRASE
        self.base_url = BITGET_BASE.rstrip("/")
        self.paper = PAPER_MODE or not self.api_key

    def _sign(self, timestamp: str, method: str, path: str, query: str = "", body: str = "") -> str:
        prehash = f"{timestamp}{method}{path}{query}{body}"
        return base64.b64encode(
            hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()

    def _headers(self, method: str, path: str, query: str = "", body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": self._sign(ts, method, path, query, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    def _request(self, method: str, path: str, query: str = "", body: dict = None) -> dict:
        if self.paper:
            # Only block write endpoints in paper mode — market data reads are free
            write_endpoints = ["/api/v2/mix/order/place-order", "/api/v2/mix/order/close-position",
                               "/api/v2/mix/order/place-trailing-stop"]
            if any(wep in path for wep in write_endpoints):
                return {"code": "00000", "msg": "paper", "data": {}}
            # Market data reads pass through to live API

        body_str = json.dumps(body) if body else ""
        url = f"{self.base_url}{path}"
        if query:
            url += f"?{query}"

        req = urllib.request.Request(
            url,
            data=body_str.encode() if body_str else None,
            headers=self._headers(method, path, query, body_str),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode() if e.fp else str(e)
            log.error(f"Bitget HTTP {e.code}: {err[:200]}")
            return {"code": str(e.code), "msg": err[:200]}
        except Exception as e:
            log.error(f"Bitget error: {e}")
            return {"code": "-1", "msg": str(e)}

    def get_balance(self) -> float:
        """Get USDT futures balance."""
        resp = self._request("GET", "/api/v2/account/info")
        if resp.get("code") == "00000":
            # Extract balance from account info
            try:
                return float(resp.get("data", {}).get("usdtEquity", 0))
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    def get_positions(self) -> List[Dict]:
        """Get open futures positions."""
        resp = self._request("GET", "/api/v2/mix/position/all-position", query="productType=umcbl")
        if resp.get("code") == "00000":
            return resp.get("data", [])
        return []

    def place_order(self, symbol: str, side: str, size: float,
                    order_type: str = "market", leverage: int = 5,
                    stop_loss_price: Optional[float] = None) -> Dict:
        """Place an order. In paper mode, simulates."""
        symbol_fmt = f"{symbol}{FUTURES_SUFFIX}"

        if self.paper:
            # Simulate order
            price = self._get_mark_price(symbol)
            order = {
                "order_id": f"paper_{int(time.time()*1000)}",
                "symbol": symbol,
                "side": side,
                "size": size,
                "price": price,
                "leverage": leverage,
                "stop_loss": stop_loss_price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "paper": True,
            }
            log.info(f"PAPER ORDER: {side} {size} {symbol} @ ~{price}")
            return {"code": "00000", "data": order}

        # Real order
        body = {
            "symbol": symbol_fmt,
            "marginCoin": "USDT",
            "side": "open_long" if side == "buy" else "open_short",
            "orderType": order_type,
            "size": str(size),
            "leverage": str(leverage),
        }
        if stop_loss_price:
            body["presetStopSurplusPrice"] = str(stop_loss_price)

        resp = self._request("POST", "/api/v2/mix/order/place-order", body=body)
        if resp.get("code") != "00000":
            log.error(f"Order failed: {resp.get('msg')}")
        return resp

    def close_position(self, symbol: str, size: Optional[float] = None) -> Dict:
        """Close a position."""
        symbol_fmt = f"{symbol}{FUTURES_SUFFIX}"
        body = {"symbol": symbol_fmt, "marginCoin": "USDT"}
        if size:
            body["size"] = str(size)

        if self.paper:
            log.info(f"PAPER CLOSE: {symbol} size={size or 'all'}")
            return {"code": "00000", "data": {"paper": True}}

        resp = self._request("POST", "/api/v2/mix/order/close-position", body=body)
        return resp

    def place_trailing_stop(self, symbol: str, side: str, size: float,
                            trigger_price: float, range_rate_pct: float) -> Dict:
        """Place a trailing stop order."""
        symbol_fmt = f"{symbol}{FUTURES_SUFFIX}"

        if self.paper:
            log.info(f"PAPER TRAIL: {symbol} trigger={trigger_price:.4f} range={range_rate_pct:.4f}%")
            return {"code": "00000", "data": {"paper": True}}

        body = {
            "symbol": symbol_fmt,
            "marginCoin": "USDT",
            "triggerPrice": str(trigger_price),
            "rangeRate": str(range_rate_pct / 100),  # Convert back to ratio
            "side": "close_long" if side == "sell" else "close_short",
            "size": str(size),
            "reduceOnly": True,
        }
        resp = self._request("POST", "/api/v2/mix/order/place-trailing-stop", body=body)
        return resp

    def _get_mark_price(self, symbol: str) -> float:
        """Get mark price (simulated in paper mode)."""
        path = f"/api/v2/mix/market/symbol-price"
        query = f"symbol={symbol}{FUTURES_SUFFIX}"
        resp = self._request("GET", path, query=query)
        try:
            return float(resp.get("data", {}).get("markPrice", 0))
        except (ValueError, TypeError):
            # Fallback for paper mode — use a reasonable default
            btc_sats = {
                "BTCUSDT": 67000, "ETHUSDT": 3500, "SOLUSDT": 175,
                "BNBUSDT": 600, "XRPUSDT": 0.55, "DOGEUSDT": 0.12,
                "UNIUSDT": 8.5, "NEARUSDT": 7.5, "AVAXUSDT": 35,
            }
            return btc_sats.get(symbol, 10.0)

bitget = BitgetClient()

# ═══════════════════════════════════════════════════════════════
# MARKET DATA FETCH
# ═══════════════════════════════════════════════════════════════

def fetch_klines(symbol: str, interval: str, limit: int = 500) -> List[Dict]:
    """Fetch klines from Bitget."""
    path = "/api/v2/mix/market/candles"
    gran_map = {"4H": "4h", "1H": "1h", "15m": "15m", "1D": "1d"}
    granularity = gran_map.get(interval, "1h")
    query = f"symbol={symbol}{FUTURES_SUFFIX}&granularity={granularity}&limit={limit}"
    resp = bitget._request("GET", path, query=query)
    if resp.get("code") == "00000":
        candles = resp.get("data", [])
        # Bitget returns [ts, open, high, low, close, vol, vol_quote]
        return [
            {
                "ts": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "vol_quote": float(c[6]),
            }
            for c in sorted(candles, key=lambda x: x[0])
        ]
    log.warning(f"Failed klines for {symbol} {interval}")
    return []

def fetch_fear_greed() -> int:
    """Fetch Fear & Greed Index from alternative.me."""
    try:
        req = urllib.request.Request("https://api.alternative.me/fng/?limit=1")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return int(data["data"][0]["value"])
    except Exception:
        return 50  # Neutral fallback

def fetch_btc_dominance_delta() -> float:
    """Fetch BTC dominance 24h change from CoinGecko."""
    try:
        req = urllib.request.Request("https://api.coingecko.com/api/v3/global")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return float(data["data"]["market_cap_percentage"]["btc_dominance_24h_change"])
    except Exception:
        return 0.0  # Assume no change on failure — fall open

# ═══════════════════════════════════════════════════════════════
# INDICATORS & ZONE DETECTION (Simplified v40 Engine)
# ═══════════════════════════════════════════════════════════════

def ema(data: List[float], period: int) -> List[float]:
    """Exponential moving average."""
    if len(data) < period:
        return [None] * len(data)
    result = [None] * len(data)
    multiplier = 2.0 / (period + 1)
    result[period - 1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        result[i] = (data[i] - result[i-1]) * multiplier + result[i-1]
    return result

def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """Average True Range."""
    a = [None] * len(closes)
    tr = [max(h - l, abs(h - (closes[i-1] if i > 0 else closes[0])),
              abs(l - (closes[i-1] if i > 0 else closes[0])))
          for i, (h, l) in enumerate(zip(highs, lows))]
    for i in range(period - 1, len(tr)):
        a[i] = sum(tr[i-period+1:i+1]) / period
    return a

def rsi(closes: List[float], period: int = 14) -> List[float]:
    """Relative Strength Index."""
    rs = [None] * len(closes)
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if avg_loss == 0:
            rs[i] = 100.0
        else:
            rs[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        avg_gain = (avg_gain * (period - 1) + gains[i-1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i-1]) / period
    return rs

def detect_support_zones(bars: List[Dict], min_pivots: int = 4) -> List[Dict]:
    """Detect 4H support zones using pivot lows (simplified v40 logic)."""
    if len(bars) < 50:
        return []

    lows = [b["low"] for b in bars]
    highs = [b["high"] for b in bars]
    closes = [b["close"] for b in bars]

    # Find pivot lows: point where low < both neighbours (±3 bar window)
    pivots = []
    window = 3
    for i in range(window, len(lows) - window):
        neighbourhood = lows[i-window:i+window+1]
        if lows[i] == min(neighbourhood) and neighbourhood.count(lows[i]) == 1:
            pivots.append({
                "index": i,
                "price": lows[i],
                "ts": bars[i]["ts"],
            })

    if len(pivots) < min_pivots:
        return []

    # Cluster pivots into zones (within 2% of each other)
    pivots.sort(key=lambda p: p["price"])
    zones = []
    used = set()

    for i, p in enumerate(pivots):
        if i in used:
            continue
        cluster = [p]
        used.add(i)
        for j in range(i + 1, len(pivots)):
            if j in used:
                continue
            if abs(pivots[j]["price"] - p["price"]) / p["price"] < 0.02:
                cluster.append(pivots[j])
                used.add(j)

        strength = min(len(cluster), 7)  # Cap at 7
        if strength >= min_pivots:
            prices = sorted([c["price"] for c in cluster])
            zone_upper = prices[-1]
            zone_lower = prices[0]
            zones.append({
                "strength": strength,
                "upper": zone_upper,
                "lower": zone_lower,
                "mid": (zone_upper + zone_lower) / 2,
                "pivots": len(cluster),
                "retests": strength,
            })

    return sorted(zones, key=lambda z: -z["strength"])

# ═══════════════════════════════════════════════════════════════
# PRE-FILTER GATE
# ═══════════════════════════════════════════════════════════════

def check_gate() -> Tuple[bool, str]:
    """Run pre-filter gate. Returns (gate_open, reason)."""
    # Fear & Greed
    fng = fetch_fear_greed()
    if fng < 10 or fng > 85:
        return False, f"Fear & Greed out of range: {fng} (need 10-85)"

    # BTC dominance 24h change — flag if large shift
    btc_dom_delta = fetch_btc_dominance_delta()
    dom_threshold = CONFIG.get("btc_dominance_delta_max", 3.0)
    if abs(btc_dom_delta) > dom_threshold:
        return False, f"BTC dominance delta {btc_dom_delta:.1f}% exceeds {dom_threshold}%"

    # Funding rate check — spot check BTC
    btc_klines = fetch_klines("BTCUSDT", "4H", limit=2)
    if btc_klines:
        # Funding check: use Bitget funding rate endpoint
        path = "/api/v2/mix/market/current-funding-rate"
        query = f"symbol=BTCUSDT{FUTURES_SUFFIX}"
        resp = bitget._request("GET", path, query=query)
        if resp.get("code") == "00000":
            try:
                funding_rate = float(resp.get("data", {}).get("fundingRate", 0))
                if abs(funding_rate) > 0.001:
                    return False, f"Funding rate extreme: {funding_rate:.4f} (±0.1% max)"
            except (ValueError, TypeError):
                pass

    return True, "Gate open ✅"

# ═══════════════════════════════════════════════════════════════
# MARKET REGIME DETECTION
# ═══════════════════════════════════════════════════════════════

def detect_regime() -> Tuple[float, str]:
    """Detect market regime using EMA50/200 cross on BTC 1H."""
    bars = fetch_klines("BTCUSDT", "1H", limit=250)
    if len(bars) < 200:
        return 0.0, "UNKNOWN"

    closes = [b["close"] for b in bars]
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)

    if ema200[-1] is None or ema50[-1] is None:
        return 0.0, "UNKNOWN"

    # Primary: EMA50 vs EMA200
    bull = ema50[-1] > ema200[-1]
    score = 30.0 if bull else -30.0

    # EMA200 slope (normalised)
    if ema200[-10] and ema200[-10] > 0:
        slope = (ema200[-1] / ema200[-10]) - 1.0
        score += math.copysign(min(abs(slope) * 100, 15), slope)

    # Price vs EMA200
    price_vs_ema200 = (closes[-1] / ema200[-1] - 1.0) * 100
    if price_vs_ema200 > 15:
        score += 10
    elif price_vs_ema200 < -15:
        score -= 10

    # HH/HL pattern
    highs = [b["high"] for b in bars[-50:]]
    lows = [b["low"] for b in bars[-50:]]
    hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
    ll = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i-1])
    if hh > ll * 1.5:
        score += 8
    elif ll > hh * 1.5:
        score -= 8

    # Label
    if score >= 20:
        label = "BULL"
    elif score <= -20:
        label = "BEAR"
    else:
        label = "SIDEWAYS"

    return score, label

# ═══════════════════════════════════════════════════════════════
# ENTRY SIGNAL EVALUATION
# ═══════════════════════════════════════════════════════════════

def evaluate_entry(symbol: str) -> Optional[Dict]:
    """Evaluate a single symbol for entry. Returns signal dict or None."""
    # Fetch data
    bars_4h = fetch_klines(symbol, "4H", limit=200)
    bars_1h = fetch_klines(symbol, "1H", limit=100)
    bars_15m = fetch_klines(symbol, "15m", limit=50)

    if len(bars_4h) < 50 or len(bars_1h) < 30 or len(bars_15m) < 20:
        return None

    # Check cooldown
    last_entry_bar = state.cooldowns.get(symbol, 0)
    current_bar = bars_15m[-1]["ts"]
    elapsed = (current_bar - last_entry_bar) / (15 * 60 * 1000) if last_entry_bar else COOLDOWN_BARS + 1
    if elapsed < COOLDOWN_BARS:
        return None

    # Detect zones
    zones = detect_support_zones(bars_4h, ZS_MIN)
    if not zones:
        return None

    # Current price
    current_price = bars_15m[-1]["close"]

    # Find the best zone the price is near
    best_zone = None
    for z in zones:
        if current_price <= z["upper"] * (1 + 0.01) and current_price >= z["lower"] * 0.99:
            best_zone = z
            break

    if not best_zone:
        return None

    # 1H RSI check
    closes_1h = [b["close"] for b in bars_1h]
    rsi_vals = rsi(closes_1h, 14)
    rsi_now = rsi_vals[-1] if rsi_vals[-1] is not None else 50
    if rsi_now < 30 or rsi_now > 70:
        return None

    # 15m bull candle
    last_candle = bars_15m[-1]
    if last_candle["close"] <= last_candle["open"]:
        return None

    # Correlation filter
    if symbol in CORRELATED:
        btc_bars = fetch_klines("BTCUSDT", "1D", limit=2)
        if len(btc_bars) >= 2:
            btc_return = (btc_bars[-1]["close"] / btc_bars[-2]["close"] - 1.0) * 100
            if btc_return <= BTC_CORR_THRESHOLD:
                log.info(f"Correlation filter: skipping {symbol} (BTC return {btc_return:.2f}%)")
                return None

    # Calculate position size
    zone_width = best_zone["upper"] - best_zone["lower"]
    sl_distance = zone_width + (zone_width * 0.5)  # SL below zone lower
    sl_price = best_zone["lower"] - (zone_width * 0.5)

    # RR check
    if sl_distance <= 0:
        return None
    potential_rr = (current_price - sl_price) / sl_distance
    if potential_rr < MIN_RR:
        return None

    # Confidence scoring
    confidence = best_zone["strength"]  # 4-7

    # Regime sizing
    sizing_mult = 1.0
    if state.regime_score >= 40:
        sizing_mult = 1.5
        direction = "LONG"
    elif state.regime_score >= 20:
        sizing_mult = 1.0
        direction = "LONG"
    elif state.regime_score >= -19:
        sizing_mult = 0.75
        direction = "LONG"
    elif state.regime_score >= -39:
        sizing_mult = 0.5
        direction = "SKIP"
    else:
        return None  # Strong BEAR — skip

    if direction == "SKIP":
        return None

    # Risk from curve
    risk_curve = {6: 0.025, 7: 0.031, 8: 0.05, 9: 0.088, 10: 0.10}
    risk_pct = risk_curve.get(confidence, DEFAULT_RISK_PCT)
    risk_pct *= sizing_mult
    risk_pct = min(risk_pct, MAX_TOTAL_RISK)

    # Position size
    risk_amount = state.free_balance * risk_pct
    notional = risk_amount * DEFAULT_LEV
    size = notional / current_price

    # Notional cap
    notional_cap = state.free_balance * NOTIONAL_CAP_MULT
    if notional > notional_cap:
        notional = notional_cap
        size = notional / current_price
        risk_amount = notional / DEFAULT_LEV

    # Adequate ATR check
    atr_vals = atr([b["high"] for b in bars_4h], [b["low"] for b in bars_4h],
                   [b["close"] for b in bars_4h], 14)
    atr_now = atr_vals[-1] if atr_vals[-1] is not None else 0
    min_atr = zone_width * 0.3
    if atr_now < min_atr:
        return None

    # Trail params
    trail_activate_price = current_price + (TRAIL_ACTIVATE_R * sl_distance)
    trail_distance_pct = (TRAIL_WIDTH_R * sl_distance) / current_price * 100

    # Apply slippage to entry price (paper-mode realism: LONG pays more to enter)
    slippage = get_slippage(symbol)
    effective_entry = current_price * (1 + slippage)

    return {
        "symbol": symbol,
        "direction": "LONG",
        "entry_price": effective_entry,
        "sl_price": sl_price,
        "sl_distance": sl_distance,
        "size": round(size, 6),
        "notional": round(notional, 2),
        "risk_amount": round(risk_amount, 2),
        "confidence": confidence,
        "zone_strength": best_zone["strength"],
        "zone_upper": best_zone["upper"],
        "zone_lower": best_zone["lower"],
        "rsi_1h": rsi_now,
        "rr_potential": round(potential_rr, 2),
        "leverage": DEFAULT_LEV,
        "trail_activate": round(trail_activate_price, 4),
        "trail_distance_pct": round(trail_distance_pct, 4),
        "regime_score": state.regime_score,
        "regime_label": state.regime_label,
    }

# ═══════════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def manage_positions(bars_15m: Dict[str, List[Dict]]) -> List[Dict]:
    """Check open positions for exits (trail hit, SL hit, time exit)."""
    exits = []

    for pos_id, pos in list(state.positions.items()):
        sym = pos["symbol"]
        bars = bars_15m.get(sym, [])
        if not bars:
            continue

        current_price = bars[-1]["close"]
        current_low = bars[-1]["low"]
        current_high = bars[-1]["high"]
        pos["bars_held"] = pos.get("bars_held", 0) + 1

        exit_reason = None
        exit_price = None

        # Check SL hit
        if pos["direction"] == "LONG":
            if current_low <= pos["sl_price"]:
                exit_reason = "sl"
                exit_price = min(current_low, pos["sl_price"])
        else:  # SHORT
            if current_high >= pos["sl_price"]:
                exit_reason = "sl"
                exit_price = max(current_high, pos["sl_price"])

        # Check trail activation (only if SL not already hit)
        if not exit_reason and pos["direction"] == "LONG":
            peak = max(pos.get("trail_peak", pos["entry_price"]), current_high)
            pos["trail_peak"] = peak
            r_units = (peak - pos["entry_price"]) / pos["sl_distance"]

            if r_units >= TRAIL_ACTIVATE_R:
                trail_level = peak - (TRAIL_WIDTH_R * pos["sl_distance"])
                pos["trail_active"] = True
                pos["trail_level"] = trail_level

            if pos.get("trail_active") and pos.get("trail_level"):
                if current_low <= pos["trail_level"]:
                    exit_reason = "trail"
                    exit_price = min(current_low, pos["trail_level"])

        # Time exit
        if pos["bars_held"] >= TIME_EXIT_BARS:
            exit_reason = "time"
            exit_price = current_price

        if exit_reason:
            exit_price = exit_price or current_price

            # Apply slippage to exit price (paper-mode realism)
            slippage = get_slippage(sym)
            if pos["direction"] == "LONG":
                effective_exit = exit_price * (1 - slippage)
            else:
                effective_exit = exit_price * (1 + slippage)

            pnl = (effective_exit - pos["entry_price"]) * pos["size"]
            if pos["direction"] == "SHORT":
                pnl = -pnl

            # Apply fees
            fee_entry = ENTRY_FEE_BLENDED * pos["notional"]
            fee_exit = EXIT_FEE_BLENDED * pos["notional"]
            pnl -= (fee_entry + fee_exit)

            # Apply funding fees (0.01% per 8h settlement)
            hours_held = pos["bars_held"] * 0.25  # 15m bars → hours
            funding_intervals = int(hours_held / 8)
            funding_cost = FUNDING_PER_8H * pos["notional"] * funding_intervals
            pnl -= funding_cost

            state.balance += pnl
            state.locked_margin -= pos["margin_used"]

            # Update streaks
            if pnl > 0:
                state.win_streak += 1
                state.loss_streak = 0
            else:
                state.loss_streak += 1
                state.win_streak = 0

            trade_record = {
                "symbol": sym,
                "direction": pos["direction"],
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "size": pos["size"],
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / pos["margin_used"] * 100, 2) if pos["margin_used"] else 0,
                "bars_held": pos["bars_held"],
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "r_multiple": round(pnl / pos["risk_amount"], 2) if pos["risk_amount"] else 0,
            }
            state.trade_history.append(trade_record)
            exits.append(trade_record)

            # Save trade log
            _save_trade(trade_record)

            del state.positions[pos_id]
            state.monthly_trades += 1
            log.info(f"EXIT {sym}: {exit_reason} | P&L ${pnl:.2f}")

    return exits

def enter_trade(signal: Dict) -> Optional[str]:
    """Enter a trade from a signal. Returns position ID or None."""
    # Position count check
    if len(state.positions) >= MAX_POS:
        return None

    # Monthly limit check
    if state.monthly_trades >= state.max_monthly_trades:
        log.info(f"Monthly trade limit reached ({state.monthly_trades}/{state.max_monthly_trades})")
        return None

    # Total risk check
    current_risk = state.balance * MAX_TOTAL_RISK
    if state.locked_margin + signal["risk_amount"] > current_risk:
        log.info(f"Total risk cap reached")
        return None

    sym = signal["symbol"]
    pos_id = f"{sym}_{int(time.time()*1000)}"

    margin = signal["notional"] / signal["leverage"]

    pos = {
        "id": pos_id,
        "symbol": sym,
        "direction": signal["direction"],
        "entry_price": signal["entry_price"],
        "sl_price": signal["sl_price"],
        "sl_distance": signal["sl_distance"],
        "size": signal["size"],
        "notional": signal["notional"],
        "risk_amount": signal["risk_amount"],
        "margin_used": margin,
        "leverage": signal["leverage"],
        "confidence": signal["confidence"],
        "trail_peak": signal["entry_price"],
        "trail_active": False,
        "trail_level": None,
        "bars_held": 0,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "zone_strength": signal["zone_strength"],
        "rsi_1h": signal["rsi_1h"],
    }

    state.positions[pos_id] = pos
    state.locked_margin += margin
    state.cooldowns[sym] = int(time.time() * 1000)

    # Place order
    side = "buy" if signal["direction"] == "LONG" else "sell"
    order_resp = bitget.place_order(
        sym, side, signal["size"],
        leverage=signal["leverage"],
        stop_loss_price=signal["sl_price"],
    )

    if order_resp.get("code") != "00000" and not PAPER_MODE:
        del state.positions[pos_id]
        state.locked_margin -= margin
        log.error(f"Order failed for {sym}: {order_resp.get('msg')}")
        return None

    # Place trailing stop
    trail_side = "sell" if signal["direction"] == "LONG" else "buy"
    bitget.place_trailing_stop(
        sym, trail_side, signal["size"],
        signal["trail_activate"], signal["trail_distance_pct"],
    )

    log.info(f"ENTER {sym} LONG @ {signal['entry_price']:.4f} | Risk ${signal['risk_amount']:.2f} | ZS {signal['zone_strength']}")
    return pos_id

# ═══════════════════════════════════════════════════════════════
# TRADE LOGGING & SNAPSHOTS
# ═══════════════════════════════════════════════════════════════

def _save_trade(trade: Dict):
    """Append a closed trade to the daily trade CSV."""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(TRADES_DIR, f"trades_{date_str}.csv")
    file_exists = os.path.exists(path)

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "symbol", "direction", "entry_price", "exit_price", "exit_reason",
            "size", "pnl", "pnl_pct", "bars_held", "entry_time", "exit_time", "r_multiple",
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow(trade)

def save_daily_snapshot():
    """Save balance snapshot for performance tracking."""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(PERF_DIR, f"balance_{date_str}.json")

    snapshot = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": round(state.balance, 2),
        "free_balance": round(state.free_balance, 2),
        "locked_margin": round(state.locked_margin, 2),
        "open_positions": len(state.positions),
        "monthly_trades": state.monthly_trades,
        "pnl_today": round(state.daily_pnl, 2),
        "regime_score": round(state.regime_score, 1),
        "regime_label": state.regime_label,
        "mode": "PAPER" if PAPER_MODE else "LIVE",
    }
    state.daily_snapshots.append(snapshot)

    # Also write aggregated daily snapshot
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)

    state.daily_pnl = 0.0  # Reset daily counter

# ═══════════════════════════════════════════════════════════════
# COMMAND HANDLERS (Data Providers for SB Agent)
# ═══════════════════════════════════════════════════════════════

def cmd_status() -> Dict:
    """Generate /status response data."""
    positions_list = []
    total_upnl = 0.0

    for pos in state.positions.values():
        bars = fetch_klines(pos["symbol"], "15m", limit=2)
        current_price = bars[-1]["close"] if bars else pos["entry_price"]
        upnl = (current_price - pos["entry_price"]) * pos["size"]
        if pos["direction"] == "SHORT":
            upnl = -upnl
        total_upnl += upnl

        positions_list.append({
            "symbol": pos["symbol"],
            "direction": pos["direction"],
            "entry": round(pos["entry_price"], 4),
            "current": round(current_price, 4),
            "upnl": round(upnl, 2),
            "sl": round(pos["sl_price"], 4),
            "bars_held": pos["bars_held"],
            "zone_strength": pos["zone_strength"],
            "trail_active": pos["trail_active"],
        })

    return {
        "mode": "PAPER" if PAPER_MODE else "LIVE",
        "balance": round(state.balance, 2),
        "free_balance": round(state.free_balance, 2),
        "locked_margin": round(state.locked_margin, 2),
        "pnl_pct": round(state.pnl_pct, 2),
        "open_positions": len(state.positions),
        "max_positions": MAX_POS,
        "positions": positions_list,
        "total_upnl": round(total_upnl, 2),
        "regime_score": round(state.regime_score, 1),
        "regime_label": state.regime_label,
        "monthly_trades": state.monthly_trades,
        "monthly_limit": state.max_monthly_trades,
        "win_streak": state.win_streak,
        "loss_streak": state.loss_streak,
        "gate_open": state.gate_open,
        "gate_reason": state.gate_reason,
    }

def cmd_positions() -> List[Dict]:
    """Generate /positions response data."""
    result = []
    for pos in state.positions.values():
        bars = fetch_klines(pos["symbol"], "15m", limit=2)
        current_price = bars[-1]["close"] if bars else pos["entry_price"]
        upnl = (current_price - pos["entry_price"]) * pos["size"]
        if pos["direction"] == "SHORT":
            upnl = -upnl

        result.append({
            "symbol": pos["symbol"],
            "dir": pos["direction"][0],  # L or S
            "entry": round(pos["entry_price"], 4),
            "current": round(current_price, 4),
            "upnl": round(upnl, 2),
            "sl": round(pos["sl_price"], 4),
            "trail": round(pos["trail_level"], 4) if pos["trail_active"] else None,
            "bars": pos["bars_held"],
            "confidence": pos["confidence"],
        })
    return result

def cmd_pnl(period: str = "daily") -> Dict:
    """Generate /pnl response data."""
    now = datetime.now(timezone.utc)
    history = state.trade_history

    if period == "daily":
        cutoff = now - timedelta(days=1)
    elif period == "weekly":
        cutoff = now - timedelta(days=7)
    elif period == "monthly":
        cutoff = now - timedelta(days=30)
    else:
        cutoff = now - timedelta(days=1)

    period_trades = [t for t in history if t["entry_time"] >= cutoff.isoformat()]
    total_pnl = sum(t["pnl"] for t in period_trades)
    wins = [t for t in period_trades if t["pnl"] > 0]
    losses = [t for t in period_trades if t["pnl"] <= 0]

    return {
        "period": period,
        "total_pnl": round(total_pnl, 2),
        "trade_count": len(period_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(period_trades) * 100, 1) if period_trades else 0,
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "best_trade": round(max((t["pnl"] for t in period_trades), default=0), 2),
        "worst_trade": round(min((t["pnl"] for t in period_trades), default=0), 2),
        "balance": round(state.balance, 2),
    }

def cmd_history(limit: int = 10) -> List[Dict]:
    """Generate /history response data (last N trades)."""
    return state.trade_history[-limit:]

def cmd_go_live() -> Dict:
    """Handle 'go live' command."""
    global PAPER_MODE
    if not PAPER_MODE:
        return {"success": False, "message": "Already in LIVE mode, Chief 🦈"}
    if not BITGET_API_KEY:
        return {"success": False, "message": "Bitget API keys not configured. Run the setup wizard first."}
    PAPER_MODE = False
    CONFIG["paper_trading"] = False
    with open(CONFIG_PATH, "w") as f:
        json.dump(CONFIG, f, indent=2)
    log.warning("MODE SWITCH: PAPER → LIVE")
    return {"success": True, "message": "🟢 LIVE MODE ACTIVATED. Real orders from now on. Let's go! 🦈"}

def cmd_go_paper() -> Dict:
    """Handle 'go paper' command."""
    global PAPER_MODE
    if PAPER_MODE:
        return {"success": False, "message": "Already in paper mode, Boss 🦈"}
    PAPER_MODE = True
    CONFIG["paper_trading"] = True
    with open(CONFIG_PATH, "w") as f:
        json.dump(CONFIG, f, indent=2)
    log.warning("MODE SWITCH: LIVE → PAPER")
    return {"success": True, "message": "🔶 Back to paper trading. Real orders stopped. 🦈"}

# ═══════════════════════════════════════════════════════════════
# POOL ROTATION (D2 — Weekly Volume Rotation)
# ═══════════════════════════════════════════════════════════════

POOL_STATE_PATH = os.path.join(SB_DIR, "pool_state.json")

def rotate_pool() -> List[str]:
    """D2 pool rotation: rank by 30-day volume, select top POOL_SIZE.
    Always includes BTCUSDT. Saves state for next rotation."""
    # Check when we last rotated
    last_rotation = 0
    if os.path.exists(POOL_STATE_PATH):
        try:
            with open(POOL_STATE_PATH) as f:
                ps = json.load(f)
                last_rotation = ps.get("last_rotation_ts", 0)
        except Exception:
            pass

    now = int(time.time())
    # Rotate weekly (604800 seconds)
    if now - last_rotation < 604800:
        # Load existing pool
        if os.path.exists(POOL_STATE_PATH):
            try:
                with open(POOL_STATE_PATH) as f:
                    return json.load(f).get("pool", DEFAULT_UNIVERSE[:POOL_SIZE])
            except Exception:
                pass

    # Calculate volume proxy for each coin in universe
    rankings = []
    for sym in DEFAULT_UNIVERSE:
        bars = fetch_klines(sym, "15m", limit=2880)  # 30 days of 15m bars
        if len(bars) < 100:
            rankings.append((sym, 0))
        else:
            vol_proxy = sum(b["volume"] * b["close"] for b in bars)
            rankings.append((sym, vol_proxy))

    rankings.sort(key=lambda x: -x[1])
    pool = [r[0] for r in rankings[:POOL_SIZE]]

    # Ensure BTC is always in
    if "BTCUSDT" not in pool:
        pool[-1] = "BTCUSDT"

    # Save pool state
    ps = {"last_rotation_ts": now, "pool": pool, "rankings": rankings[:POOL_SIZE]}
    with open(POOL_STATE_PATH, "w") as f:
        json.dump(ps, f, indent=2)

    log.info(f"Pool rotated: {len(pool)} coins in D2 pool")
    return pool

# ═══════════════════════════════════════════════════════════════
# MAIN TICK — Called every 15m bar
# ═══════════════════════════════════════════════════════════════

def tick() -> Dict:
    """Execute one trading cycle. Returns summary dict for SB agent."""
    global state

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "PAPER" if PAPER_MODE else "LIVE",
        "gate_open": True,
        "gate_reason": "",
        "regime_score": 0.0,
        "regime_label": "UNKNOWN",
        "signals": [],
        "entries": [],
        "exits": [],
        "positions_open": 0,
        "balance": 0.0,
    }

    # If LIVE, sync balance from exchange
    if not PAPER_MODE:
        live_balance = bitget.get_balance()
        if live_balance > 0:
            state.balance = live_balance

    # Pre-filter gate
    gate_open, gate_reason = check_gate()
    state.gate_open = gate_open
    state.gate_reason = gate_reason
    result["gate_open"] = gate_open
    result["gate_reason"] = gate_reason

    if not gate_open:
        log.info(f"Gate closed: {gate_reason}")
        result["positions_open"] = len(state.positions)
        result["balance"] = round(state.balance, 2)
        return result

    # Regime detection
    regime_score, regime_label = detect_regime()
    state.regime_score = regime_score
    state.regime_label = regime_label
    result["regime_score"] = round(regime_score, 1)
    result["regime_label"] = regime_label

    # Pool rotation (check if due)
    state.pool = rotate_pool()

    # Fetch 15m data for all pool members and manage existing positions
    bars_15m_cache = {}
    for sym in list(state.positions.keys()):
        bars = fetch_klines(sym, "15m", limit=50)
        if bars:
            bars_15m_cache[sym] = bars

    # Manage existing positions (check exits)
    exits = manage_positions(bars_15m_cache)
    result["exits"] = exits

    # Evaluate new entries on pool members
    signals = []
    for sym in state.pool:
        if sym in state.positions:
            continue  # Already in a position
        if len(state.positions) >= MAX_POS:
            break

        signal = evaluate_entry(sym)
        if signal:
            signals.append(signal)
            pos_id = enter_trade(signal)
            if pos_id:
                result["entries"].append({
                    "symbol": sym,
                    "entry_price": signal["entry_price"],
                    "sl_price": signal["sl_price"],
                    "risk": signal["risk_amount"],
                    "confidence": signal["confidence"],
                    "pos_id": pos_id,
                })

    result["signals"] = signals
    result["positions_open"] = len(state.positions)
    result["balance"] = round(state.balance, 2)

    # Save state
    state._save_state()
    log.info(f"Tick complete: {len(signals)} signals, {len(result['entries'])} entries, {len(exits)} exits")

    return result

# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINTS
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Steve Bot Trading Engine")
    parser.add_argument("command", nargs="?", default="tick",
                        choices=["tick", "status", "positions", "pnl", "history", "go_live", "go_paper"])
    parser.add_argument("--period", default="daily")
    parser.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()

    if args.command == "tick":
        result = tick()
        print(json.dumps(result, indent=2))
    elif args.command == "status":
        print(json.dumps(cmd_status(), indent=2))
    elif args.command == "positions":
        print(json.dumps(cmd_positions(), indent=2))
    elif args.command == "pnl":
        print(json.dumps(cmd_pnl(args.period), indent=2))
    elif args.command == "history":
        print(json.dumps(cmd_history(args.limit), indent=2))
    elif args.command == "go_live":
        print(json.dumps(cmd_go_live(), indent=2))
    elif args.command == "go_paper":
        print(json.dumps(cmd_go_paper(), indent=2))
