from fastapi import FastAPI
import uvicorn
import urllib.request
import json
import time
from pathlib import Path
from typing import Any, Dict, List

APP = FastAPI(title="stream-coordinator-test")

WORKERS = [
    {"symbol": "AUDNZD_otc", "url": "http://127.0.0.1:8011/status"},
    {"symbol": "EURCHF_otc", "url": "http://127.0.0.1:8013/status"},
    {"symbol": "CHFJPY_otc", "url": "http://127.0.0.1:8014/status"},
]

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return float(default)


def _structure_bias_from_closed(closed: List[Dict[str, Any]], window: int = 16) -> Dict[str, Any]:
    tail = list(closed or [])[-window:]
    if len(tail) < 4:
        return {
            "structure_bias": "CHAOS",
            "reason": "not_enough_candles",
            "window": window,
        }

    closes = [_safe_float(x.get("close")) for x in tail]
    highs = [_safe_float(x.get("high")) for x in tail]
    lows = [_safe_float(x.get("low")) for x in tail]

    close_up = 0
    close_down = 0
    high_up = 0
    high_down = 0
    low_up = 0
    low_down = 0

    for i in range(1, len(tail)):
        if closes[i] > closes[i - 1]:
            close_up += 1
        elif closes[i] < closes[i - 1]:
            close_down += 1

        if highs[i] > highs[i - 1]:
            high_up += 1
        elif highs[i] < highs[i - 1]:
            high_down += 1

        if lows[i] > lows[i - 1]:
            low_up += 1
        elif lows[i] < lows[i - 1]:
            low_down += 1

    net_move = closes[-1] - closes[0]
    close_score = close_up - close_down
    high_score = high_up - high_down
    low_score = low_up - low_down
    hl_score = high_score + low_score

    dirs = []
    body_ratios = []
    ranges = []
    for c in tail:
        o = _safe_float(c.get("open"))
        cl = _safe_float(c.get("close"))
        h = _safe_float(c.get("high"))
        l = _safe_float(c.get("low"))
        rng = abs(h - l)
        body = abs(cl - o)
        dirs.append("BUY" if cl >= o else "SELL")
        body_ratios.append((body / rng) if rng > 0 else 0.0)
        ranges.append(rng)

    alternations = 0
    for i in range(1, len(dirs)):
        if dirs[i] != dirs[i - 1]:
            alternations += 1

    avg_body_ratio = sum(body_ratios) / len(body_ratios) if body_ratios else 0.0
    avg_range = sum(ranges) / len(ranges) if ranges else 0.0
    full_range = (max(highs) - min(lows)) if highs and lows else 0.0
    compression_ratio = (avg_range / full_range) if full_range > 0 else 0.0

    # Trend strength score - zatim jen debug, neridi vstupy.
    trend_strength = 0

    abs_hl = abs(hl_score)
    abs_net = abs(net_move)

    if abs_hl >= 10:
        trend_strength += 35
    elif abs_hl >= 6:
        trend_strength += 25
    elif abs_hl >= 3:
        trend_strength += 15

    if abs_net > 0:
        trend_strength += min(25, int(abs_net / max(avg_range, 0.000001) * 8))

    if avg_body_ratio >= 0.55:
        trend_strength += 15
    elif avg_body_ratio >= 0.45:
        trend_strength += 10
    elif avg_body_ratio >= 0.35:
        trend_strength += 5

    if alternations >= 8:
        trend_strength -= 35
    elif alternations >= 6:
        trend_strength -= 25
    elif alternations >= 4:
        trend_strength -= 10

    if compression_ratio >= 0.45:
        trend_strength -= 10

    trend_strength = max(0, min(100, trend_strength))

    clean_allowed = (
        alternations <= 4
        and avg_body_ratio >= 0.50
    )

    if trend_strength >= 70 and clean_allowed:
        trend_phase = "STRONG"
    elif trend_strength >= 45 and clean_allowed:
        trend_phase = "CLEAN"
    elif trend_strength >= 25:
        trend_phase = "EARLY"
    else:
        trend_phase = "CHAOS"

    bias = "CHAOS"
    reason = "structure_mixed"

    # Structure-first logic:
    # hlavni je posun high/low struktury, ne pocet BUY/SELL svíček.
    # close_score je jen pomocny údaj.
    if hl_score >= 6 and net_move > 0:
        bias = "BUY"
        reason = "strong_higher_high_low_structure"
    elif hl_score <= -6 and net_move < 0:
        bias = "SELL"
        reason = "strong_lower_high_low_structure"
    elif hl_score >= 3 and net_move > 0:
        bias = "BUY"
        reason = "weak_higher_high_low_structure"
    elif hl_score <= -3 and net_move < 0:
        bias = "SELL"
        reason = "weak_lower_high_low_structure"

    # Human filter: kdyz se svicky moc stridaji, neni to cisty trend.
    # Ale silna HH/HL struktura + silny net_move ma prednost pred alternations.
    strong_buy_structure = (hl_score >= 10 and net_move > 0)
    strong_sell_structure = (hl_score <= -10 and net_move < 0)

    if alternations >= 6 and not (strong_buy_structure or strong_sell_structure):
        bias = "CHAOS"
        reason = "too_many_alternations"

    return {
        "structure_bias": bias,
        "reason": reason,
        "window": window,
        "close_score": close_score,
        "high_score": high_score,
        "low_score": low_score,
        "hl_score": hl_score,
        "net_move": round(net_move, 6),
        "trend_strength": trend_strength,
        "trend_phase": trend_phase,
        "quality_debug": {
            "avg_body_ratio": round(avg_body_ratio, 3),
            "alternations": alternations,
            "avg_range": round(avg_range, 6),
            "full_range": round(full_range, 6),
            "compression_ratio": round(compression_ratio, 3),
            "dirs_tail": dirs[-6:],
        },
        "close_up": close_up,
        "close_down": close_down,
        "high_up": high_up,
        "high_down": high_down,
        "low_up": low_up,
        "low_down": low_down,
        "first_close": closes[0],
        "last_close": closes[-1],
    }


def _market_state_from_worker_item(item: Dict[str, Any]) -> Dict[str, Any]:
    if not item.get("worker_ok"):
        return {
            "symbol": item.get("symbol"),
            "state": "CHAOS",
            "reason": "worker_not_ok",
        }

    active_mode = str(item.get("active_mode") or "")
    stream_closed_len = int(item.get("stream_closed_len") or 0)
    recent_closed = list(item.get("recent_closed") or [])
    current = item.get("current") or {}

    if active_mode != "STREAM":
        return {
            "symbol": item.get("symbol"),
            "state": "CHAOS",
            "reason": "not_in_stream",
        }

    if stream_closed_len < 3 or len(recent_closed) < 3:
        return {
            "symbol": item.get("symbol"),
            "state": "CHAOS",
            "reason": "not_enough_recent_closed_stream_candles",
        }

    def candle_info(c: Dict[str, Any]) -> Dict[str, Any]:
        o = _safe_float(c.get("open"))
        cl = _safe_float(c.get("close"))
        h = _safe_float(c.get("high"))
        l = _safe_float(c.get("low"))

        body = abs(cl - o)
        rng = abs(h - l)
        body_ratio = (body / rng) if rng > 0 else 0.0

        upper_wick = max(0.0, h - max(o, cl))
        lower_wick = max(0.0, min(o, cl) - l)

        upper_wick_ratio = (upper_wick / rng) if rng > 0 else 0.0
        lower_wick_ratio = (lower_wick / rng) if rng > 0 else 0.0

        direction = "BUY" if cl >= o else "SELL"

        return {
            "dir": direction,
            "body_ratio": body_ratio,
            "upper_wick_ratio": upper_wick_ratio,
            "lower_wick_ratio": lower_wick_ratio,
            "open": o,
            "close": cl,
            "high": h,
            "low": l,
        }

    def is_cross(x: Dict[str, Any]) -> bool:
        return (
            x["upper_wick_ratio"] > 0.0
            and x["lower_wick_ratio"] > 0.0
        )

    def is_anchor_ok(x: Dict[str, Any]) -> bool:
        return (not is_cross(x)) and x["body_ratio"] >= 0.30

    def reaction_buy_ok(x: Dict[str, Any]) -> bool:
        return (
            x["dir"] == "BUY"
            and x["body_ratio"] >= 0.40
            and x["lower_wick_ratio"] <= 0.22
        )

    def reaction_sell_ok(x: Dict[str, Any]) -> bool:
        return (
            x["dir"] == "SELL"
            and x["body_ratio"] >= 0.40
            and x["upper_wick_ratio"] <= 0.22
        )

    infos = [candle_info(c) for c in recent_closed[-12:]]
    cur = candle_info(current) if current else None
    cur_dir = cur["dir"] if cur else "NONE"
    cur_body_ratio = cur["body_ratio"] if cur else 0.0

    debug = {
        "closed_count_used": len(infos),
        "cur_dir": cur_dir,
        "cur_body_ratio": round(cur_body_ratio, 2),
        "reaction_buy_ok": reaction_buy_ok(cur) if cur else False,
        "reaction_sell_ok": reaction_sell_ok(cur) if cur else False,
    }

    latest_state = "CHAOS"
    latest_reason = "no_valid_sequence"
    anchor = None
    cross_count = 0

    for idx, x in enumerate(infos):
        x_cross = is_cross(x)
        x_anchor_ok = is_anchor_ok(x)

        if anchor is None:
            if x_anchor_ok:
                anchor = x
                cross_count = 0
                latest_state = f"VYTVÁŘÍ SE {anchor['dir']}"
                latest_reason = f"anchor_started dir={anchor['dir']} body={anchor['body_ratio']:.2f} idx={idx}"
            continue

        # mame anchor a jedeme sekvenci
        if x_cross:
            cross_count += 1

            if cross_count == 1:
                latest_state = f"VYTVÁŘÍ SE {anchor['dir']}"
                latest_reason = f"first_cross_after_{anchor['dir'].lower()}_anchor anchor_body={anchor['body_ratio']:.2f} idx={idx}"

            elif cross_count == 2:
                if anchor["dir"] == "BUY":
                    latest_state = "R2CB"
                    latest_reason = f"two_crosses_after_buy_anchor anchor_body={anchor['body_ratio']:.2f} idx={idx}"
                else:
                    latest_state = "R2CS"
                    latest_reason = f"two_crosses_after_sell_anchor anchor_body={anchor['body_ratio']:.2f} idx={idx}"

            else:
                latest_state = "ZRUŠENO"
                latest_reason = f"three_crosses_in_row anchor_dir={anchor['dir']} idx={idx}"

            continue

        # non-cross po anchoru
        if cross_count >= 2:
            # po 2 krizcich prisla zavrena normalni svicka -> zacina novy cyklus od ni, pokud je silna
            if x_anchor_ok:
                anchor = x
                cross_count = 0
                latest_state = f"VYTVÁŘÍ SE {anchor['dir']}"
                latest_reason = f"new_anchor_after_old_sequence dir={anchor['dir']} body={anchor['body_ratio']:.2f} idx={idx}"
            else:
                anchor = None
                cross_count = 0
                latest_state = "ZRUŠENO"
                latest_reason = f"sequence_broken_after_two_crosses idx={idx}"
            continue

        # jeste nejsou 2 krizky
        if x_anchor_ok:
            anchor = x
            cross_count = 0
            latest_state = f"VYTVÁŘÍ SE {anchor['dir']}"
            latest_reason = f"anchor_replaced dir={anchor['dir']} body={anchor['body_ratio']:.2f} idx={idx}"
        else:
            latest_state = "CHAOS"
            latest_reason = f"weak_non_cross_without_sequence idx={idx}"
            anchor = None
            cross_count = 0

    debug.update({
        "seq_anchor_dir": anchor["dir"] if anchor else None,
        "seq_anchor_body_ratio": round(anchor["body_ratio"], 2) if anchor else None,
        "seq_cross_count": cross_count,
        "seq_last_state": latest_state,
        "seq_last_reason": latest_reason,
    })

    # FINAL jen kdyz posledni zavrene vytvorily R2CB/R2CS a current to potvrdi
    if latest_state == "R2CB":
        if cur and reaction_buy_ok(cur):
            return {
                "symbol": item.get("symbol"),
                "state": "FINAL BUY",
                "reason": f"{latest_reason} cur={cur_dir}:{cur_body_ratio:.2f}",
                "debug": debug,
            }
        return {
            "symbol": item.get("symbol"),
            "state": "R2CB",
            "reason": f"{latest_reason} cur={cur_dir}:{cur_body_ratio:.2f}",
            "debug": debug,
        }

    if latest_state == "R2CS":
        if cur and reaction_sell_ok(cur):
            return {
                "symbol": item.get("symbol"),
                "state": "FINAL SELL",
                "reason": f"{latest_reason} cur={cur_dir}:{cur_body_ratio:.2f}",
                "debug": debug,
            }
        return {
            "symbol": item.get("symbol"),
            "state": "R2CS",
            "reason": f"{latest_reason} cur={cur_dir}:{cur_body_ratio:.2f}",
            "debug": debug,
        }

    if latest_state == "ZRUŠENO":
        return {
            "symbol": item.get("symbol"),
            "state": "ZRUŠENO",
            "reason": latest_reason,
            "debug": debug,
        }

    if latest_state in ("VYTVÁŘÍ SE BUY", "VYTVÁŘÍ SE SELL"):
        # pojistka proti silne svicce proti smeru
        if latest_state == "VYTVÁŘÍ SE BUY" and cur and cur_dir == "SELL" and cur_body_ratio >= 0.45:
            return {
                "symbol": item.get("symbol"),
                "state": "CHAOS",
                "reason": f"closed_buy_but_current_strong_sell cur_body_ratio={cur_body_ratio:.2f}",
                "debug": debug,
            }
        if latest_state == "VYTVÁŘÍ SE SELL" and cur and cur_dir == "BUY" and cur_body_ratio >= 0.45:
            return {
                "symbol": item.get("symbol"),
                "state": "CHAOS",
                "reason": f"closed_sell_but_current_strong_buy cur_body_ratio={cur_body_ratio:.2f}",
                "debug": debug,
            }
        return {
            "symbol": item.get("symbol"),
            "state": latest_state,
            "reason": f"{latest_reason} cur={cur_dir}:{cur_body_ratio:.2f}",
            "debug": debug,
        }

    return {
        "symbol": item.get("symbol"),
        "state": "CHAOS",
        "reason": latest_reason,
        "debug": debug,
    }

def fetch_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@APP.get("/status")
def status():
    items = []
    now = time.time()

    for w in WORKERS:
        data = fetch_json(w["url"])
        item = {
            "symbol": w["symbol"],
            "worker_ok": bool(data.get("ok")) if isinstance(data, dict) else False,
            "worker_url": w["url"],
            "fetched_at": now,
        }

        if isinstance(data, dict) and data.get("ok"):
            item.update({
                "active_mode": data.get("active_mode"),
                "stream_closed_len": data.get("stream_closed_len"),
                "has_stream_current": data.get("has_stream_current"),
                "tick_feed": data.get("tick_feed"),
                "last_closed": data.get("last_closed"),
                "recent_closed": data.get("recent_closed"),
                "current": data.get("current"),
            })
        else:
            item["error"] = data.get("error", "worker_unavailable") if isinstance(data, dict) else "invalid_worker_response"

        items.append(item)

    return {
        "ok": True,
        "count": len(items),
        "items": items,
    }

@APP.get("/api/coord/status")
def coord_status():
    raw = status()
    simple = []
    for item in raw["items"]:
        simple.append({
            "symbol": item["symbol"],
            "worker_ok": item.get("worker_ok"),
            "active_mode": item.get("active_mode"),
            "stream_closed_len": item.get("stream_closed_len"),
            "has_stream_current": item.get("has_stream_current"),
        })
    return {
        "ok": True,
        "items": simple,
    }

@APP.get("/api/coord/market_state")
def coord_market_state():
    raw = status()
    items: List[Dict[str, Any]] = []
    for item in raw["items"]:
        items.append(_market_state_from_worker_item(item))
    return {
        "ok": True,
        "items": items,
    }

def _candle_bias_info(c: Dict[str, Any]) -> Dict[str, Any]:
    o = _safe_float(c.get("open"))
    cl = _safe_float(c.get("close"))
    h = _safe_float(c.get("high"))
    l = _safe_float(c.get("low"))

    body = abs(cl - o)
    rng = abs(h - l)
    body_ratio = (body / rng) if rng > 0 else 0.0

    upper_wick = max(0.0, h - max(o, cl))
    lower_wick = max(0.0, min(o, cl) - l)

    upper_wick_ratio = (upper_wick / rng) if rng > 0 else 0.0
    lower_wick_ratio = (lower_wick / rng) if rng > 0 else 0.0

    direction = "BUY" if cl >= o else "SELL"

    return {
        "dir": direction,
        "body_ratio": body_ratio,
        "upper_wick_ratio": upper_wick_ratio,
        "lower_wick_ratio": lower_wick_ratio,
        "open": o,
        "close": cl,
        "high": h,
        "low": l,
    }


def _window_bias(closed: List[Dict[str, Any]], window: int) -> Dict[str, Any]:
    tail = list(closed[-window:]) if closed else []
    if len(tail) < window:
        return {
            "window": window,
            "bias": "CHAOS",
            "reason": "not_enough_window_data",
            "buy_count": 0,
            "sell_count": 0,
            "strong_buy": 0,
            "strong_sell": 0,
            "cross_count": 0,
            "alternations": 0,
            "net_move": 0.0,
        }

    infos = [_candle_bias_info(c) for c in tail]
    dirs = [x["dir"] for x in infos]

    buy_count = dirs.count("BUY")
    sell_count = dirs.count("SELL")

    strong_buy = sum(1 for x in infos if x["dir"] == "BUY" and x["body_ratio"] >= 0.30)
    strong_sell = sum(1 for x in infos if x["dir"] == "SELL" and x["body_ratio"] >= 0.30)

    cross_count = sum(
        1 for x in infos
        if x["upper_wick_ratio"] > 0.0 and x["lower_wick_ratio"] > 0.0
    )

    alternations = 0
    for i in range(1, len(dirs)):
        if dirs[i] != dirs[i - 1]:
            alternations += 1

    net_move = _safe_float(tail[-1].get("close")) - _safe_float(tail[0].get("open"))

    bias = "CHAOS"
    reason = "mixed_or_weak"

    if (
        buy_count >= sell_count + 2
        and strong_buy >= strong_sell
        and net_move > 0
        and alternations <= max(3, window // 2)
    ):
        bias = "BUY"
        reason = "dominant_buy_clean"
    elif (
        sell_count >= buy_count + 2
        and strong_sell >= strong_buy
        and net_move < 0
        and alternations <= max(3, window // 2)
    ):
        bias = "SELL"
        reason = "dominant_sell_clean"

    return {
        "window": window,
        "bias": bias,
        "reason": reason,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "strong_buy": strong_buy,
        "strong_sell": strong_sell,
        "cross_count": cross_count,
        "alternations": alternations,
        "net_move": round(net_move, 6),
    }


def _market_bias_from_closed(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    windows = [
        _window_bias(closed, 8),
        _window_bias(closed, 12),
        _window_bias(closed, 16),
    ]

    buy_votes = sum(1 for w in windows if w["bias"] == "BUY")
    sell_votes = sum(1 for w in windows if w["bias"] == "SELL")
    chaos_votes = sum(1 for w in windows if w["bias"] == "CHAOS")

    if buy_votes >= 2 and buy_votes > sell_votes:
        market_bias = "BUY"
    elif sell_votes >= 2 and sell_votes > buy_votes:
        market_bias = "SELL"
    else:
        market_bias = "CHAOS"

    return {
        "market_bias": market_bias,
        "market_bias_reason": f"buy_votes={buy_votes},sell_votes={sell_votes},chaos_votes={chaos_votes}",
        "market_bias_windows": windows,
    }


def _simple_state_from_internal_and_bias(internal_state: str, market_bias: str) -> str:
    s = str(internal_state or "").strip().upper()
    b = str(market_bias or "").strip().upper()

    if s == "ZRUŠENO":
        return "ZRUŠENO"

    if s == "FINAL BUY" and b == "BUY":
        return "VSTUP BUY"
    if s == "FINAL SELL" and b == "SELL":
        return "VSTUP SELL"

    if s in ("VYTVÁŘÍ SE BUY", "R2CB", "BUY") and b == "BUY":
        return "POZOR BUY"
    if s in ("VYTVÁŘÍ SE SELL", "R2CS", "SELL") and b == "SELL":
        return "POZOR SELL"

    return "NIC"



def _build_trade_confidence(zaver, s8, p8, sc8, s12, p12, sc12, s16, p16, sc16):
    scores = [int(sc8 or 0), int(sc12 or 0), int(sc16 or 0)]
    max_score = max(scores)
    avg_score = int(sum(scores) / max(1, len(scores)))
    chaos_count = [p8, p12, p16].count("CHAOS")

    base = 20

    if "CLEAN BUY" in str(zaver) or "CLEAN SELL" in str(zaver):
        base = 82
    elif "PULLBACK" in str(zaver):
        base = 72
    elif "BUY" in str(zaver) or "SELL" in str(zaver):
        base = 60
    elif "CHAOS" in str(zaver):
        base = 20

    conf = base + int(max_score * 0.25) + int(avg_score * 0.15)
    conf -= chaos_count * 10

    if "CHAOS" in str(zaver):
        conf = min(conf, 35)

    return max(0, min(99, int(conf)))


def _build_zaver(s8, p8, sc8, s12, p12, sc12, s16, p16, sc16):
    buy_count = sum(1 for x in [s8, s12, s16] if x == "BUY")
    sell_count = sum(1 for x in [s8, s12, s16] if x == "SELL")

    strong_buy_context = (
        s16 == "BUY"
        or s12 == "BUY"
        or (p16 in ("EARLY", "CLEAN", "STRONG") and sc16 >= 35)
        or (p12 in ("EARLY", "CLEAN", "STRONG") and sc12 >= 35)
    )

    strong_sell_context = (
        s16 == "SELL"
        or s12 == "SELL"
        or (p16 in ("EARLY", "CLEAN", "STRONG") and sc16 >= 35)
        or (p12 in ("EARLY", "CLEAN", "STRONG") and sc12 >= 35)
    )

    clean_buy = (
        buy_count >= 2
        and ("CLEAN" in [p8, p12, p16] or "STRONG" in [p8, p12, p16])
        and max(sc8, sc12, sc16) >= 45
    )

    clean_sell = (
        sell_count >= 2
        and ("CLEAN" in [p8, p12, p16] or "STRONG" in [p8, p12, p16])
        and max(sc8, sc12, sc16) >= 45
    )

    if clean_buy:
        return "🟢 CLEAN BUY"

    if clean_sell:
        return "🔴 CLEAN SELL"

    # BUY pullback:
    # higher context still BUY, but short window is weak/against trend
    if (
        strong_buy_context
        and s8 in ("SELL", "CHAOS")
        and sell_count <= 1
    ):
        return "🟦 BUY PULLBACK"

    # SELL pullback:
    # higher context still SELL, but short window is weak/against trend
    if (
        strong_sell_context
        and s8 in ("BUY", "CHAOS")
        and buy_count <= 1
    ):
        return "🟪 SELL PULLBACK"

    buy_ok = (
        buy_count >= 2
        and max(sc8, sc12, sc16) >= 35
        and [p8, p12, p16].count("CHAOS") <= 1
    )

    sell_ok = (
        sell_count >= 2
        and max(sc8, sc12, sc16) >= 35
        and [p8, p12, p16].count("CHAOS") <= 1
    )

    if buy_ok:
        return "🟡 BUY"

    if sell_ok:
        return "🟠 SELL"

    return "⚫ CHAOS"


_STRUCTURE_DEBUG_CACHE = None
_STRUCTURE_DEBUG_CACHE_TS = 0.0
_STRUCTURE_DEBUG_CACHE_TTL = 60.0


@APP.get("/api/coord/structure_debug")
def coord_structure_debug():
    global _STRUCTURE_DEBUG_CACHE, _STRUCTURE_DEBUG_CACHE_TS

    now = time.time()
    if _STRUCTURE_DEBUG_CACHE is not None and (now - _STRUCTURE_DEBUG_CACHE_TS) < _STRUCTURE_DEBUG_CACHE_TTL:
        return _STRUCTURE_DEBUG_CACHE

    items = []
    for w in WORKERS:
        data = fetch_json(w["url"])
        if not isinstance(data, dict) or not data.get("ok"):
            items.append({
                "symbol": w["symbol"],
                "ok": False,
                "error": data.get("error", "worker_not_ok") if isinstance(data, dict) else "invalid_worker_response",
            })
            continue

        recent_closed = list(data.get("recent_closed") or [])
        s8 = _structure_bias_from_closed(recent_closed, 8)
        s12 = _structure_bias_from_closed(recent_closed, 12)
        s16 = _structure_bias_from_closed(recent_closed, 16)

        zaver = _build_zaver(
            s8.get("structure_bias"),
            s8.get("trend_phase"),
            s8.get("trend_strength", 0),

            s12.get("structure_bias"),
            s12.get("trend_phase"),
            s12.get("trend_strength", 0),

            s16.get("structure_bias"),
            s16.get("trend_phase"),
            s16.get("trend_strength", 0),
        )

        trade_confidence = _build_trade_confidence(
            zaver,
            s8.get("structure_bias"),
            s8.get("trend_phase"),
            s8.get("trend_strength", 0),

            s12.get("structure_bias"),
            s12.get("trend_phase"),
            s12.get("trend_strength", 0),

            s16.get("structure_bias"),
            s16.get("trend_phase"),
            s16.get("trend_strength", 0),
        )

        items.append({
            "symbol": w["symbol"],
            "ok": True,
            "active_mode": data.get("active_mode"),
            "stream_closed_len": data.get("stream_closed_len"),
            "structure_8": s8,
            "structure_12": s12,
            "structure_16": s16,
            "zaver": zaver,
            "trade_confidence": trade_confidence,
        })

    tradable_items = [
        item for item in items
        if item.get("ok")
        and item.get("active_mode") == "STREAM"
        and "CHAOS" not in str(item.get("zaver"))
    ]

    best_setup = None
    if tradable_items:
        best_setup = max(
            tradable_items,
            key=lambda item: int(item.get("trade_confidence") or 0)
        )

    if best_setup:
        try:
            log_path = Path("best_setup_history.log")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            log_path.open("a", encoding="utf-8").write(
                f"{ts} | {best_setup.get('symbol')} | {best_setup.get('active_mode')} | "
                f"{best_setup.get('zaver')} | {best_setup.get('trade_confidence')}%\\n"
            )
        except Exception:
            pass

    result = {
        "ok": True,
        "cache_ttl_sec": int(_STRUCTURE_DEBUG_CACHE_TTL),
        "cache_age_sec": 0,
        "best_setup": best_setup,
        "items": items,
    }
    _STRUCTURE_DEBUG_CACHE = result
    _STRUCTURE_DEBUG_CACHE_TS = time.time()
    return result


@APP.get("/api/coord/simple_state")
def coord_simple_state():
    raw_workers = status()
    raw_items = list(raw_workers.get("items", []) or [])
    internal_items = coord_market_state().get("items", []) or []
    internal_by_symbol = {str(x.get("symbol")): x for x in internal_items}

    items = []
    for raw_item in raw_items:
        symbol = str(raw_item.get("symbol") or "")
        internal = internal_by_symbol.get(symbol, {})

        bias_info = _market_bias_from_closed(list(raw_item.get("recent_closed") or []))
        market_bias = bias_info["market_bias"]

        internal_state = str(internal.get("state") or "")
        items.append({
            "symbol": symbol,
            "simple_state": _simple_state_from_internal_and_bias(internal_state, market_bias),
            "internal_state": internal_state,
            "market_bias": market_bias,
            "market_bias_reason": bias_info["market_bias_reason"],
            "market_bias_windows": bias_info["market_bias_windows"],
            "reason": internal.get("reason"),
        })

    return {
        "ok": True,
        "items": items,
    }

def main():
    uvicorn.run(APP, host="127.0.0.1", port=8020, log_level="info")

if __name__ == "__main__":
    main()
