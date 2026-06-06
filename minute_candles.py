import os
import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional, List, Tuple

from pocketoptionapi_async import AsyncPocketOptionClient
from pocketoptionapi_async.exceptions import PocketOptionError, WebSocketError


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except BrokenPipeError:
        pass


def fmt_ts(ts: int) -> str:
    utc = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    loc = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S LOCAL")
    return f"{utc} | {loc}"


def _to_dict(obj: Any) -> Optional[dict]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            return None
    if hasattr(obj, "__dict__"):
        try:
            return dict(obj.__dict__)
        except Exception:
            return None
    return None


def _parse_int(v: Any) -> Optional[int]:
    try:
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v.strip())
    except Exception:
        pass
    return None


def _parse_float(v: Any) -> Optional[float]:
    try:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            return float(v.strip().replace(",", "."))
    except Exception:
        pass
    return None


def _normalize_timestamp(value: Any) -> Optional[int]:
    """
    Returns UNIX seconds.

    Supports:
    - datetime.datetime (naive/aware)
    - UNIX ms -> s
    - minutes since epoch -> seconds (heuristic)
    - UNIX seconds
    """
    if isinstance(value, datetime):
        try:
            # naive datetime -> treated as LOCAL by Python
            return int(value.timestamp())
        except Exception:
            return None

    x = _parse_int(value)
    if x is None:
        return None

    if x > 2_000_000_000_000:
        x //= 1000

    if 20_000_000 <= x <= 80_000_000:  # minute-epoch
        return x * 60

    if x >= 1_500_000_000:
        return x

    return None


def extract_open_ts_raw(candle: Any) -> Optional[int]:
    d = _to_dict(candle) or {}
    keys = [
        "timestamp", "time", "t", "at",
        "from_", "from",
        "open_time", "start_time", "begin", "begin_time",
        "date", "datetime", "ts",
        "open_ts", "start_ts",
        "server_time", "srv_time",
    ]

    for k in keys:
        if k in d:
            ts = _normalize_timestamp(d.get(k))
            if ts is not None:
                return ts
        if hasattr(candle, k):
            try:
                ts = _normalize_timestamp(getattr(candle, k))
                if ts is not None:
                    return ts
            except Exception:
                pass

    if isinstance(candle, (list, tuple)) and candle:
        ts = _normalize_timestamp(candle[0])
        if ts is not None:
            return ts
        if isinstance(candle[0], dict):
            for k in keys:
                if k in candle[0]:
                    ts = _normalize_timestamp(candle[0].get(k))
                    if ts is not None:
                        return ts

    for k, v in d.items():
        kl = str(k).lower()
        if any(s in kl for s in ("time", "date", "ts")):
            ts = _normalize_timestamp(v)
            if ts is not None:
                return ts

    return None


def extract_ohlc(candle: Any) -> Tuple[float, float, float, float]:
    d = _to_dict(candle) or {}

    def g(keys: List[str]) -> Optional[float]:
        for k in keys:
            if k in d:
                v = _parse_float(d[k])
                if v is not None:
                    return v
            if hasattr(candle, k):
                try:
                    v = _parse_float(getattr(candle, k))
                    if v is not None:
                        return v
                except Exception:
                    pass
        return None

    o = g(["open", "o", "openPrice", "open_price", "op"])
    h = g(["high", "h", "highPrice", "high_price", "hp"])
    l = g(["low", "l", "lowPrice", "low_price", "lp"])
    c = g(["close", "c", "closePrice", "close_price", "cp"])

    if None not in (o, h, l, c):
        return float(o), float(h), float(l), float(c)

    if isinstance(candle, (list, tuple)) and len(candle) >= 5:
        v1 = [_parse_float(x) for x in candle[1:5]]
        if None not in v1:
            a, b, c1, d1 = v1
            o1, cl1, h1, l1 = a, b, c1, d1
            if h1 >= max(o1, cl1) and l1 <= min(o1, cl1):
                return float(o1), float(h1), float(l1), float(cl1)
            o2, h2, l2, cl2 = a, b, c1, d1
            if h2 >= max(o2, cl2) and l2 <= min(o2, cl2):
                return float(o2), float(h2), float(l2), float(cl2)

    raise KeyError("Could not extract OHLC from candle")


def _floor_tf(ts: int, tf: int) -> int:
    return (ts // tf) * tf


def _compute_open_ts_from_now(now: int, tf: int, require_closed: bool) -> int:
    """
    Robust alignment:
    - if we require CLOSED candles, take last fully closed candle => previous tf bucket
    - else take current tf bucket (may be forming)
    """
    base = _floor_tf(now, tf)
    if require_closed:
        return base - tf
    return base


async def connect_client(ssid: str, is_demo: bool) -> AsyncPocketOptionClient:
    client = AsyncPocketOptionClient(ssid, is_demo=is_demo, enable_logging=False)
    await client.connect()
    return client


async def main():
    po_ssid = os.getenv("PO_SSID", "").strip()
    if not po_ssid:
        raise SystemExit("Missing PO_SSID.")

    is_demo = _env_bool("PO_IS_DEMO", True)
    tf = int(os.getenv("PO_TF_SEC", "60").strip())
    poll_sec = float(os.getenv("PO_POLL_SEC", "1.0").strip())
    count = int(os.getenv("PO_CANDLE_COUNT", "20").strip())
    asset = os.getenv("PO_ASSET", "CHFJPY_otc").strip()

    require_closed = _env_bool("PO_REQUIRE_CLOSED", True)

    _safe_print(f"[boot] demo={is_demo} tf={tf}s poll={poll_sec}s count={count} asset={asset} require_closed={require_closed}")

    backoff = 1.0
    backoff_max = 10.0

    client: Optional[AsyncPocketOptionClient] = None
    last_open_ts: Optional[int] = None

    try:
        while True:
            try:
                if client is None:
                    client = await connect_client(po_ssid, is_demo=is_demo)
                    _safe_print("[ok] connected")
                    backoff = 1.0

                candles = await client.get_candles(asset, tf, count)
                if not candles:
                    await asyncio.sleep(poll_sec)
                    continue

                last = candles[-1]
                o, h, l, c = extract_ohlc(last)

                now = int(time.time())
                open_ts = _compute_open_ts_from_now(now, tf, require_closed=require_closed)
                close_ts = open_ts + tf

                if last_open_ts == open_ts:
                    await asyncio.sleep(poll_sec)
                    continue
                last_open_ts = open_ts

                # debug only
                raw_ts = extract_open_ts_raw(last)
                drift_sec = (raw_ts - now) if raw_ts is not None else None

                _safe_print(
                    f"[candle] asset={asset} tf={tf} open_ts={open_ts} close_ts={close_ts} "
                    f"O={o:.3f} H={h:.3f} L={l:.3f} C={c:.3f}"
                )
                _safe_print(f"         NOW  : {fmt_ts(now)}")
                _safe_print(f"         OPEN : {fmt_ts(open_ts)}")
                _safe_print(f"         CLOSE: {fmt_ts(close_ts)}")
                if raw_ts is not None:
                    _safe_print(f"         API_TS(raw): {fmt_ts(raw_ts)} drift_sec={drift_sec}")

                await asyncio.sleep(poll_sec)

            except (WebSocketError, PocketOptionError) as e:
                _safe_print(f"[warn] {type(e).__name__}: {e}")
                try:
                    if client is not None:
                        await client.close()
                except Exception:
                    pass
                client = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, backoff_max)

            except Exception as e:
                _safe_print(f"[warn] {type(e).__name__}: {e}")
                await asyncio.sleep(1.0)

    except (KeyboardInterrupt, asyncio.CancelledError):
        try:
            if client is not None:
                await client.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

