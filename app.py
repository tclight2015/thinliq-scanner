"""
Thin Liquidity Scanner — Backend
Binance 公開 API，無需 Key
支援現貨 + 永續合約雙市場
"""

import asyncio
import json
import threading
import time
import math
from collections import defaultdict, deque
from flask import Flask, jsonify, render_template, request
import urllib.request
import urllib.error

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================
BINANCE_REST_SPOT   = "https://api.binance.com/api/v3"
BINANCE_REST_PERP   = "https://fapi.binance.com/fapi/v1"
BINANCE_REST_PERP2  = "https://fapi.binance.com/fapi/v2"

SCAN_INTERVAL       = 30        # seconds between full scans
MAX_MCAP_DEFAULT    = 500       # million USD
MIN_VOL_MULT        = 2.0
TOP_N               = 80        # how many symbols to track

# ============================================================
# SHARED STATE
# ============================================================
state = {
    "scan_results": [],
    "last_scan": 0,
    "scanning": False,
    "funding_rates": {},
    "oi_data": {},
    "ls_ratio": {},
}

# ============================================================
# HELPERS
# ============================================================
def fetch_json(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ThinLiqScanner/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[fetch] ERROR {url[:80]} → {e}")
        return None


def safe_float(v, default=0.0):
    try:
        return float(v)
    except:
        return default


# ============================================================
# BINANCE DATA FETCHERS
# ============================================================
def get_spot_tickers():
    data = fetch_json(f"{BINANCE_REST_SPOT}/ticker/24hr")
    if not data:
        return {}
    out = {}
    for t in data:
        sym = t.get("symbol", "")
        if sym.endswith("USDT"):
            coin = sym[:-4]
            out[coin] = {
                "symbol": coin,
                "market": "SPOT",
                "price": safe_float(t.get("lastPrice")),
                "change24h": safe_float(t.get("priceChangePercent")),
                "volume24h_usdt": safe_float(t.get("quoteVolume")),
                "volume_base": safe_float(t.get("volume")),
                "high24h": safe_float(t.get("highPrice")),
                "low24h": safe_float(t.get("lowPrice")),
                "count": int(t.get("count", 0)),
            }
    return out


def get_perp_tickers():
    data = fetch_json(f"{BINANCE_REST_PERP}/ticker/24hr")
    if not data:
        return {}
    out = {}
    for t in data:
        sym = t.get("symbol", "")
        if sym.endswith("USDT"):
            coin = sym[:-4]
            out[coin] = {
                "symbol": coin,
                "market": "PERP",
                "price": safe_float(t.get("lastPrice")),
                "change24h": safe_float(t.get("priceChangePercent")),
                "volume24h_usdt": safe_float(t.get("quoteVolume")),
                "volume_base": safe_float(t.get("volume")),
                "high24h": safe_float(t.get("highPrice")),
                "low24h": safe_float(t.get("lowPrice")),
                "count": int(t.get("count", 0)),
            }
    return out


def get_order_book_depth(symbol, market="SPOT", limit=20):
    """Fetch order book and compute ±2% depth in USDT"""
    if market == "SPOT":
        url = f"{BINANCE_REST_SPOT}/depth?symbol={symbol}USDT&limit={limit}"
    else:
        url = f"{BINANCE_REST_PERP}/depth?symbol={symbol}USDT&limit={limit}"

    data = fetch_json(url, timeout=5)
    if not data:
        return None

    bids = [(safe_float(p), safe_float(q)) for p, q in data.get("bids", [])]
    asks = [(safe_float(p), safe_float(q)) for p, q in data.get("asks", [])]

    if not bids or not asks:
        return None

    mid = (bids[0][0] + asks[0][0]) / 2
    spread_pct = (asks[0][0] - bids[0][0]) / mid * 100 if mid > 0 else 0

    # ±2% depth
    lower = mid * 0.98
    upper = mid * 1.02

    bid_depth = sum(p * q for p, q in bids if p >= lower)
    ask_depth = sum(p * q for p, q in asks if p <= upper)
    total_depth = bid_depth + ask_depth

    # Top 5 for display
    top_bids = [{"price": p, "size": q, "usdt": p*q} for p, q in bids[:5]]
    top_asks = [{"price": p, "size": q, "usdt": p*q} for p, q in asks[:5]]

    return {
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "total_depth_2pct": total_depth,
        "top_bids": top_bids,
        "top_asks": top_asks,
    }


def get_klines_volume_mult(symbol, market="SPOT", interval="1h", periods=24):
    """Calculate current hour volume vs average of past N periods"""
    if market == "SPOT":
        url = f"{BINANCE_REST_SPOT}/klines?symbol={symbol}USDT&interval={interval}&limit={periods+1}"
    else:
        url = f"{BINANCE_REST_PERP}/klines?symbol={symbol}USDT&interval={interval}&limit={periods+1}"

    data = fetch_json(url, timeout=5)
    if not data or len(data) < 3:
        return 1.0, 0

    # Last complete candle volume (index -2), current (index -1)
    volumes = [safe_float(k[7]) for k in data]  # quote volume (USDT)
    current_vol = volumes[-1]
    hist_avg = sum(volumes[:-1]) / len(volumes[:-1]) if volumes[:-1] else 1
    mult = current_vol / hist_avg if hist_avg > 0 else 1.0
    return round(mult, 2), round(hist_avg, 0)


def get_funding_rates():
    """Fetch all perpetual funding rates"""
    data = fetch_json(f"{BINANCE_REST_PERP}/premiumIndex")
    if not data:
        return {}
    out = {}
    for item in data:
        sym = item.get("symbol", "")
        if sym.endswith("USDT"):
            coin = sym[:-4]
            out[coin] = {
                "funding_rate": safe_float(item.get("lastFundingRate")),
                "next_funding_time": item.get("nextFundingTime", 0),
                "mark_price": safe_float(item.get("markPrice")),
                "index_price": safe_float(item.get("indexPrice")),
            }
    return out


def get_open_interest(symbol):
    url = f"{BINANCE_REST_PERP}/openInterest?symbol={symbol}USDT"
    data = fetch_json(url, timeout=5)
    if not data:
        return 0
    return safe_float(data.get("openInterest", 0))


def get_long_short_ratio(symbol):
    # Try v1 endpoint instead of v2
    url = f"{BINANCE_REST_PERP}/globalLongShortAccountRatio?symbol={symbol}USDT&period=1h&limit=2"
    data = fetch_json(url, timeout=5)
    if not data or not isinstance(data, list) or len(data) == 0:
        return None, None
    latest = data[-1]
    prev = data[-2] if len(data) > 1 else data[-1]
    current = safe_float(latest.get("longShortRatio"))
    previous = safe_float(prev.get("longShortRatio"))
    return current, previous


def get_taker_buy_ratio(symbol, market="SPOT"):
    """Recent taker buy vs sell ratio from aggTrades"""
    if market == "SPOT":
        url = f"{BINANCE_REST_SPOT}/aggTrades?symbol={symbol}USDT&limit=200"
    else:
        url = f"{BINANCE_REST_PERP}/aggTrades?symbol={symbol}USDT&limit=200"

    data = fetch_json(url, timeout=5)
    if not data:
        return 0.5

    buy_vol = sum(safe_float(t.get("q", 0)) * safe_float(t.get("p", 0))
                  for t in data if not t.get("m", True))
    sell_vol = sum(safe_float(t.get("q", 0)) * safe_float(t.get("p", 0))
                   for t in data if t.get("m", True))
    total = buy_vol + sell_vol
    return buy_vol / total if total > 0 else 0.5


# ============================================================
# SCORING ENGINE
# ============================================================
def compute_score(d):
    score = 0

    # Volume surge (0-30)
    vm = d.get("volume_mult", 1)
    score += min(vm * 4, 30)

    # Impact (0-25) — higher impact = thinner book = higher score
    impact = d.get("impact_pct", 0)
    score += min(impact * 0.2, 25)

    # Taker buy pressure (0-15)
    tbr = d.get("taker_buy_ratio", 0.5)
    if d.get("direction") == "SHORT":
        tbr = 1 - tbr  # flip for short
    if tbr > 0.55:
        score += (tbr - 0.5) * 100

    # 1h momentum (0-10)
    change1h = abs(d.get("change1h", 0))
    score += min(change1h * 1.5, 10)

    # Funding rate benefit (0-10)
    fr = d.get("funding_rate", 0)
    if d.get("direction") == "LONG" and fr < -0.05:
        score += 10  # shorts paying = good for longs
    elif d.get("direction") == "SHORT" and fr > 0.1:
        score += 10  # longs paying = good for shorts
    elif abs(fr) < 0.03:
        score += 5

    # Long/short ratio extremes (0-10)
    ls = d.get("ls_ratio", 1.0)
    if d.get("direction") == "LONG" and ls < 0.7:
        score += 10  # too many shorts = contrarian long
    elif d.get("direction") == "SHORT" and ls > 2.0:
        score += 10  # too many longs = contrarian short

    return min(max(round(score, 1), 0), 100)


def compute_direction(d):
    """Determine LONG / SHORT / WAIT based on signal confluence"""
    long_score = 0
    short_score = 0

    tbr = d.get("taker_buy_ratio", 0.5)
    fr = d.get("funding_rate", 0)
    ls = d.get("ls_ratio", 1.0)
    change24h = d.get("change24h", 0)
    vm = d.get("volume_mult", 1)

    # Taker flow
    if tbr > 0.58: long_score += 2
    elif tbr < 0.42: short_score += 2

    # Funding rate
    if fr < -0.05: long_score += 2      # shorts paying → bullish
    elif fr > 0.10: short_score += 2     # longs paying → bearish
    elif fr > 0.05: short_score += 1

    # Long/short ratio (contrarian)
    if ls < 0.8: long_score += 1         # too bearish → contrarian long
    elif ls > 2.2: short_score += 1

    # Recent momentum
    if change24h > 5: long_score += 1
    elif change24h < -5: short_score += 1

    if long_score >= 3 and long_score > short_score + 1:
        return "LONG"
    elif short_score >= 3 and short_score > long_score + 1:
        return "SHORT"
    else:
        return "WAIT"


def compute_signal_validity(d):
    """Estimate how long the signal is likely valid"""
    drivers = []
    hours = 1.0

    vm = d.get("volume_mult", 1)
    fr = abs(d.get("funding_rate", 0))
    impact = d.get("impact_pct", 0)
    ls_change = d.get("ls_ratio_change", 0)

    if vm > 5:
        drivers.append("量能突破")
        hours = max(hours, 0.5)   # fast decay
    if vm > 3:
        hours = max(hours, 1.0)

    if fr > 0.08:
        drivers.append("費率異常")
        hours = max(hours, 6.0)   # until next settlement

    if impact > 50:
        drivers.append("訂單簿極薄")
        hours = max(hours, 4.0)   # structural, slower to change

    if abs(ls_change) > 0.3:
        drivers.append("多空比異動")
        hours = max(hours, 3.0)

    if hours <= 1.0:
        urgency = "⚡ 緊迫"
        advice = "需在 30–60 分鐘內行動"
    elif hours <= 3.0:
        urgency = "⏱ 適中"
        advice = f"約 {int(hours)} 小時窗口，可分 2–3 批建倉"
    else:
        urgency = "🕐 充裕"
        advice = f"約 {int(hours)} 小時窗口，可慢慢建倉"

    return {
        "hours": hours,
        "urgency": urgency,
        "advice": advice,
        "drivers": drivers,
    }


def compute_exit_strategy(price, direction, depth_info, funding_rate):
    """Dynamic exit levels based on orderbook resistance/support"""
    if not price or price <= 0:
        return []

    if direction == "LONG":
        # Find resistance from ask side
        asks = depth_info.get("top_asks", []) if depth_info else []
        resistances = sorted([a["price"] for a in asks if a["price"] > price * 1.05])

        tp1_pct = 0.25
        tp2_pct = 0.55
        tp3_pct = 1.00
        sl_pct  = -0.15

        # Adjust TP if there's a clear resistance wall
        if resistances:
            r1 = resistances[0]
            tp1_pct = max((r1 / price - 1) * 0.9, 0.15)

        return [
            {"label": "TP1 — 第一批出場 (30%)", "pct": f"+{tp1_pct*100:.0f}%",
             "price": price * (1 + tp1_pct), "portion": "30%", "cls": "l1"},
            {"label": "TP2 — 第二批出場 (40%)", "pct": f"+{tp2_pct*100:.0f}%",
             "price": price * (1 + tp2_pct), "portion": "40%", "cls": "l2"},
            {"label": "TP3 — 最後出場 (30%)",   "pct": f"+{tp3_pct*100:.0f}%",
             "price": price * (1 + tp3_pct), "portion": "30%", "cls": "l3"},
            {"label": "SL — 停損",               "pct": f"{sl_pct*100:.0f}%",
             "price": price * (1 + sl_pct),  "portion": "全倉", "cls": "sl"},
        ]
    else:  # SHORT
        tp1_pct = -0.20
        tp2_pct = -0.45
        tp3_pct = -0.70
        sl_pct  = 0.12

        # Tighter SL if high positive funding (short friendly)
        if funding_rate > 0.10:
            sl_pct = 0.08

        return [
            {"label": "TP1 — 第一批回補 (30%)", "pct": f"{tp1_pct*100:.0f}%",
             "price": price * (1 + tp1_pct), "portion": "30%", "cls": "l1"},
            {"label": "TP2 — 第二批回補 (40%)", "pct": f"{tp2_pct*100:.0f}%",
             "price": price * (1 + tp2_pct), "portion": "40%", "cls": "l2"},
            {"label": "TP3 — 最後回補 (30%)",   "pct": f"{tp3_pct*100:.0f}%",
             "price": price * (1 + tp3_pct), "portion": "30%", "cls": "l3"},
            {"label": "SL — 停損",               "pct": f"+{sl_pct*100:.0f}%",
             "price": price * (1 + sl_pct),  "portion": "全倉", "cls": "sl"},
        ]


def compute_impact(capital, depth_total):
    """Estimate % price movement from given capital"""
    if depth_total <= 0:
        return 999.0
    raw = (capital / depth_total) * 60
    return min(round(raw, 1), 999)


# ============================================================
# CANDIDATE SELECTION
# ============================================================
def select_candidates(spot_tickers, perp_tickers, max_mcap_m, min_vol_usdt=500_000):
    """
    Pick candidates by volume filter.
    We use volume as a proxy for market cap since real mcap needs external feed.
    min_vol_usdt: exclude dead coins
    max cap proxy: exclude coins with 24h volume > max_mcap_m * 5 (rough)
    """
    candidates = []

    # Combine, prefer perp over spot for same coin
    seen = set()
    for coin, d in perp_tickers.items():
        if d["volume24h_usdt"] < min_vol_usdt:
            continue
        # rough mcap filter: exclude huge coins (vol proxy)
        if d["volume24h_usdt"] > max_mcap_m * 1_000_000 * 8:
            continue
        seen.add(coin)
        candidates.append(d)

    for coin, d in spot_tickers.items():
        if coin in seen:
            continue
        if d["volume24h_usdt"] < min_vol_usdt:
            continue
        if d["volume24h_usdt"] > max_mcap_m * 1_000_000 * 8:
            continue
        candidates.append(d)

    # Sort by volume desc, take top N
    candidates.sort(key=lambda x: x["volume24h_usdt"], reverse=True)
    return candidates[:TOP_N]


# ============================================================
# MAIN SCAN WORKER
# ============================================================
def run_full_scan(capital=50000, max_mcap_m=500, min_vol_mult=2.0, min_score=60, market_filter="both"):
    print("[scan] Starting full scan...")
    state["scanning"] = True

    try:
        # Fetch base tickers
        spot = get_spot_tickers()
        perp = get_perp_tickers()
        funding = get_funding_rates()
        state["funding_rates"] = funding

        print(f"[scan] Got {len(spot)} spot, {len(perp)} perp tickers")

        if market_filter == "spot":
            perp = {}
        elif market_filter == "perp":
            spot = {}

        candidates = select_candidates(spot, perp, max_mcap_m)
        print(f"[scan] {len(candidates)} candidates after filter")

        results = []

        for base in candidates:
            coin = base["symbol"]
            mkt  = base["market"]

            try:
                # Volume multiplier
                vol_mult, avg_vol = get_klines_volume_mult(coin, mkt, "1h", 24)
                if vol_mult < min_vol_mult:
                    continue

                # Order book
                depth = get_order_book_depth(coin, mkt, 20)
                if not depth:
                    continue

                impact = compute_impact(capital, depth["total_depth_2pct"])

                # Taker ratio
                tbr = get_taker_buy_ratio(coin, mkt)

                # Perp-specific
                fr = 0.0
                oi = 0.0
                ls_ratio = 1.0
                ls_prev = 1.0

                if mkt == "PERP":
                    fr_data = funding.get(coin, {})
                    fr = fr_data.get("funding_rate", 0.0)
                    oi = get_open_interest(coin)
                    ls_ratio, ls_prev_val = get_long_short_ratio(coin)
                    if ls_ratio is None:
                        ls_ratio = 1.0
                    ls_prev = ls_prev_val if ls_prev_val else ls_ratio

                # 1h price change estimate from high/low
                change1h = (base["price"] / base["low24h"] - 1) * 10 if base.get("low24h", 0) > 0 else 0

                d = {
                    "symbol": coin,
                    "market": mkt,
                    "price": base["price"],
                    "change24h": base["change24h"],
                    "change1h": round(change1h, 2),
                    "volume24h_usdt": base["volume24h_usdt"],
                    "volume_mult": vol_mult,
                    "avg_vol_usdt": avg_vol,
                    "depth_2pct": round(depth["total_depth_2pct"], 0),
                    "bid_depth": round(depth["bid_depth"], 0),
                    "ask_depth": round(depth["ask_depth"], 0),
                    "spread_pct": round(depth["spread_pct"], 4),
                    "impact_pct": impact,
                    "taker_buy_ratio": round(tbr, 3),
                    "funding_rate": round(fr, 6),
                    "open_interest": round(oi, 0),
                    "ls_ratio": round(ls_ratio, 3),
                    "ls_ratio_change": round(ls_ratio - ls_prev, 3),
                    "top_bids": depth["top_bids"],
                    "top_asks": depth["top_asks"],
                }

                d["direction"] = compute_direction(d)
                d["score"] = compute_score(d)

                if d["score"] < min_score:
                    continue

                d["validity"] = compute_signal_validity(d)
                d["exit_levels"] = compute_exit_strategy(
                    d["price"], d["direction"], depth, fr
                )

                # Alerts
                alerts = []
                if vol_mult > 8:
                    alerts.append({"msg": f"🔥 成交量爆增 {vol_mult:.1f}x", "type": "green"})
                if impact > 60:
                    alerts.append({"msg": f"⚡ 訂單簿極薄，衝擊估算 {impact:.0f}%", "type": "warn"})
                if mkt == "PERP" and fr > 0.15:
                    alerts.append({"msg": f"🚨 高正費率 {fr*100:.3f}%，多頭過擁擠", "type": "danger"})
                if mkt == "PERP" and fr < -0.08:
                    alerts.append({"msg": f"📉 負費率 {fr*100:.3f}%，空頭付錢", "type": "green"})
                if tbr > 0.65:
                    alerts.append({"msg": f"📈 主動買入比 {tbr*100:.0f}%，強買壓", "type": "green"})
                if tbr < 0.35:
                    alerts.append({"msg": f"📉 主動賣出比 {(1-tbr)*100:.0f}%，強賣壓", "type": "danger"})
                if mkt == "PERP" and ls_ratio > 2.5:
                    alerts.append({"msg": f"⚠ 多空比 {ls_ratio:.2f}，多頭過度擁擠", "type": "warn"})
                if mkt == "PERP" and ls_ratio < 0.6:
                    alerts.append({"msg": f"⚠ 多空比 {ls_ratio:.2f}，空頭過度擁擠", "type": "warn"})

                d["alerts"] = alerts
                results.append(d)
                print(f"[scan] ✓ {coin}/{mkt} score={d['score']} dir={d['direction']}")

            except Exception as e:
                print(f"[scan] SKIP {coin}: {e}")
                continue

        results.sort(key=lambda x: x["score"], reverse=True)
        state["scan_results"] = results
        state["last_scan"] = time.time()
        print(f"[scan] Done. {len(results)} results.")

    except Exception as e:
        print(f"[scan] FATAL: {e}")
    finally:
        state["scanning"] = False


def background_scanner():
    """Run scan every SCAN_INTERVAL seconds"""
    while True:
        if not state["scanning"]:
            run_full_scan()
        time.sleep(SCAN_INTERVAL)


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan")
def api_scan():
    capital = float(request.args.get("capital", 50000))
    max_mcap = float(request.args.get("max_mcap", 500))
    min_vol_mult = float(request.args.get("min_vol_mult", 2.0))
    min_score = float(request.args.get("min_score", 60))
    market = request.args.get("market", "both")
    force = request.args.get("force", "0") == "1"

    if force and not state["scanning"]:
        t = threading.Thread(
            target=run_full_scan,
            args=(capital, max_mcap, min_vol_mult, min_score, market),
            daemon=True
        )
        t.start()
        return jsonify({"status": "scanning", "message": "掃描啟動中..."})

    results = state["scan_results"]

    # Apply client-side filters on cached results
    filtered = [
        r for r in results
        if r.get("volume_mult", 0) >= min_vol_mult
        and r.get("score", 0) >= min_score
        and (market == "both" or r.get("market", "").upper() == market.upper())
    ]

    return jsonify({
        "status": "ok",
        "scanning": state["scanning"],
        "last_scan": state["last_scan"],
        "count": len(filtered),
        "results": filtered[:60],
    })


@app.route("/api/coin/<symbol>")
def api_coin(symbol):
    """Fetch fresh data for a single coin (for position tracker)"""
    market = request.args.get("market", "PERP").upper()
    capital = float(request.args.get("capital", 50000))

    try:
        depth = get_order_book_depth(symbol, market, 20)
        if not depth:
            return jsonify({"error": "無法取得訂單簿"}), 400

        tbr = get_taker_buy_ratio(symbol, market)
        fr = 0.0
        oi = 0.0
        ls_ratio = 1.0

        if market == "PERP":
            fr_data = state["funding_rates"].get(symbol, {})
            fr = fr_data.get("funding_rate", 0.0)
            if not fr:
                fresh = get_funding_rates()
                state["funding_rates"] = fresh
                fr = fresh.get(symbol, {}).get("funding_rate", 0.0)
            oi = get_open_interest(symbol)
            ls_ratio_val, _ = get_long_short_ratio(symbol)
            ls_ratio = ls_ratio_val if ls_ratio_val else 1.0

        # Get price
        if market == "SPOT":
            t_data = fetch_json(f"{BINANCE_REST_SPOT}/ticker/price?symbol={symbol}USDT")
        else:
            t_data = fetch_json(f"{BINANCE_REST_PERP}/ticker/price?symbol={symbol}USDT")

        price = safe_float(t_data.get("price", 0)) if t_data else depth["mid"]
        impact = compute_impact(capital, depth["total_depth_2pct"])

        d = {
            "symbol": symbol,
            "market": market,
            "price": price,
            "spread_pct": depth["spread_pct"],
            "depth_2pct": depth["total_depth_2pct"],
            "bid_depth": depth["bid_depth"],
            "ask_depth": depth["ask_depth"],
            "impact_pct": impact,
            "taker_buy_ratio": tbr,
            "funding_rate": fr,
            "open_interest": oi,
            "ls_ratio": ls_ratio,
            "top_bids": depth["top_bids"],
            "top_asks": depth["top_asks"],
        }

        d["direction"] = compute_direction(d)
        return jsonify({"status": "ok", "data": d})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/exit/<symbol>")
def api_exit(symbol):
    """Compute exit strategy given entry price and position"""
    entry_price = float(request.args.get("entry", 0))
    direction = request.args.get("direction", "LONG").upper()
    market = request.args.get("market", "PERP").upper()

    if entry_price <= 0:
        return jsonify({"error": "請輸入進場價格"}), 400

    try:
        depth = get_order_book_depth(symbol, market, 20)
        fr_data = state["funding_rates"].get(symbol, {})
        fr = fr_data.get("funding_rate", 0.0)

        exits = compute_exit_strategy(entry_price, direction, depth, fr)

        # Current price
        if market == "SPOT":
            t_data = fetch_json(f"{BINANCE_REST_SPOT}/ticker/price?symbol={symbol}USDT")
        else:
            t_data = fetch_json(f"{BINANCE_REST_PERP}/ticker/price?symbol={symbol}USDT")

        current_price = safe_float(t_data.get("price", 0)) if t_data else entry_price
        pnl_pct = (current_price / entry_price - 1) * 100 if direction == "LONG" else (entry_price / current_price - 1) * 100

        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "entry_price": entry_price,
            "current_price": current_price,
            "direction": direction,
            "pnl_pct": round(pnl_pct, 2),
            "funding_rate": fr,
            "exit_levels": exits,
            "depth_2pct": depth["total_depth_2pct"] if depth else 0,
            "spread_pct": depth["spread_pct"] if depth else 0,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/funding")
def api_funding():
    fr = state["funding_rates"]
    if not fr:
        fr = get_funding_rates()
        state["funding_rates"] = fr

    sorted_fr = sorted(
        [{"symbol": k, **v} for k, v in fr.items()],
        key=lambda x: abs(x["funding_rate"]),
        reverse=True
    )[:20]

    return jsonify({"status": "ok", "data": sorted_fr})


@app.route("/api/status")
def api_status():
    return jsonify({
        "scanning": state["scanning"],
        "last_scan": state["last_scan"],
        "result_count": len(state["scan_results"]),
    })


# ============================================================
# STARTUP
# ============================================================
if __name__ == "__main__":
    # Start background scanner
    scanner_thread = threading.Thread(target=background_scanner, daemon=True)
    scanner_thread.start()

    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
