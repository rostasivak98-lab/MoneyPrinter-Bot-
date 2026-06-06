import os
import time
import json
import asyncio
import inspect
import datetime
import logging
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pocketoptionapi_async import AsyncPocketOptionClient

# Quiet logs
logging.getLogger("pocketoptionapi_async").setLevel(logging.ERROR)
logging.getLogger("websockets").setLevel(logging.ERROR)

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

APP_NAME = "New Chance"

TF_SEC = int(os.getenv("PO_TF_SEC", "60"))
POLL_SEC = float(os.getenv("PO_POLL_SEC", "0.5"))
SYMBOL_DEFAULT = os.getenv("PO_SYMBOL", "CHFJPY_otc")
IS_DEMO = os.getenv("PO_IS_DEMO", "1").strip() in ("1", "true", "True", "yes", "YES")
LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Prague")
SSID_FILE = os.getenv("PO_SSID_FILE", "po_ssid.txt")

# When HISTORY last closed is older than this, switch to STREAM
FRESH_MAX_SEC = int(os.getenv("PO_FRESH_MAX_SEC", "120"))

# Force offset strongly recommended in your setup (fixes +2h symptom)
PO_TIME_OFFSET_SEC = int(os.getenv("PO_TIME_OFFSET_SEC", "7200"))


def _now_ts() -> int:
    return int(time.time())


def _align(ts: int) -> int:
    return ts - (ts % TF_SEC)


def _fmt(ts_epoch: int) -> str:
    try:
        if ZoneInfo:
            dt = datetime.datetime.fromtimestamp(ts_epoch, tz=ZoneInfo(LOCAL_TZ))
        else:
            dt = datetime.datetime.fromtimestamp(ts_epoch)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts_epoch)


def _open_close_str(ts_open: int) -> tuple[str, str]:
    return _fmt(ts_open), _fmt(ts_open + TF_SEC)


def _age_sec(ts_open: int) -> int:
    # how old is this CLOSED candle (close time)
    close_ts = ts_open + TF_SEC
    age = _now_ts() - close_ts
    return 0 if age < 0 else int(age)


def _read_ssid() -> Optional[str]:
    ssid_env = os.getenv("PO_SSID")
    if ssid_env and ssid_env.strip():
        return ssid_env.strip()

    try:
        with open(SSID_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    return ln
        return None
    except Exception:
        return None


@dataclass
class StreamStats:
    ticks_seen: int = 0
    last_tick_local: Optional[float] = None
    last_tick_po_ts: Optional[float] = None
    last_tick_price: Optional[float] = None


@dataclass
class State:
    running: bool = False
    connected: bool = False

    symbol: str = SYMBOL_DEFAULT
    is_demo: bool = IS_DEMO

    tf_sec: int = TF_SEC
    poll_sec: float = POLL_SEC
    local_tz: str = LOCAL_TZ
    fresh_max_sec: int = FRESH_MAX_SEC
    po_time_offset_sec: int = PO_TIME_OFFSET_SEC

    last_connect_error: str = "—"
    last_candles_error: str = "—"
    last_fetch_count: int = 0
    last_poll_ts: Optional[float] = None

    # HISTORY (closed only)
    history_closed: List[Dict[str, Any]] = None

    # STREAM (forming + closed)
    stream_current: Optional[Dict[str, Any]] = None
    stream_closed: List[Dict[str, Any]] = None
    stream: StreamStats = None

    # Hybrid
    active_mode: str = "HISTORY"  # HISTORY or STREAM
    history_age_sec: Optional[int] = None
    stream_age_sec: Optional[int] = None

    # WS RAW debug (tail of raw websocket messages)
    socket_raw_tail: List[Dict[str, Any]] = None
    socket_raw_total: int = 0
    socket_raw_last_ts: Optional[float] = None


app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATE = State(
    history_closed=[],
    stream_current=None,
    stream_closed=[],
    stream=StreamStats(),
    socket_raw_tail=[],
    socket_raw_total=0,
    socket_raw_last_ts=None,
)

_engine_task: Optional[asyncio.Task] = None
_stop_event = asyncio.Event()
_lock = asyncio.Lock()


def _pick_last_history() -> Optional[Dict[str, Any]]:
    return STATE.history_closed[-1] if STATE.history_closed else None


def _pick_last_stream_closed() -> Optional[Dict[str, Any]]:
    return STATE.stream_closed[-1] if STATE.stream_closed else None


def _update_hybrid_choice() -> None:
    h = _pick_last_history()
    s = _pick_last_stream_closed()

    STATE.history_age_sec = _age_sec(int(h["timestamp"])) if h else None
    STATE.stream_age_sec = _age_sec(int(s["timestamp"])) if s else None

    if STATE.history_age_sec is not None and STATE.history_age_sec <= STATE.fresh_max_sec:
        STATE.active_mode = "HISTORY"
    else:
        # if stream has anything, prefer it; else fallback to history
        STATE.active_mode = "STREAM" if s is not None else "HISTORY"


def _active_last_closed() -> Optional[Dict[str, Any]]:
    if STATE.active_mode == "STREAM":
        return _pick_last_stream_closed() or _pick_last_history()
    return _pick_last_history() or _pick_last_stream_closed()


def _ts_open_from_po_epoch(po_ts: float) -> int:
    # PO epoch -> our epoch by subtracting offset, then align to minute
    return _align(int(po_ts) - int(STATE.po_time_offset_sec))


def _append_stream_closed(c: Dict[str, Any]) -> None:
    lst = STATE.stream_closed or []
    lst.append(c)
    if len(lst) > 300:
        lst = lst[-300:]
    STATE.stream_closed = lst


def _on_tick(po_ts: float, price: float) -> None:
    # update stats
    STATE.stream.ticks_seen += 1
    STATE.stream.last_tick_local = time.time()
    STATE.stream.last_tick_po_ts = float(po_ts)
    STATE.stream.last_tick_price = float(price)

    ts_open = _ts_open_from_po_epoch(po_ts)
    cur = STATE.stream_current

    if cur and int(cur["timestamp"]) == ts_open:
        cur["high"] = max(cur["high"], float(price))
        cur["low"] = min(cur["low"], float(price))
        cur["close"] = float(price)
        cur["volume"] = float(cur.get("volume", 0.0) + 1.0)
        STATE.stream_current = cur
        return

    # minute rolled: close previous forming candle
    if cur and cur.get("timestamp") is not None:
        prev_ts = int(cur["timestamp"])
        o, c = _open_close_str(prev_ts)
        closed = {
            "symbol": STATE.symbol,
            "timestamp": prev_ts,
            "open": float(cur["open"]),
            "high": float(cur["high"]),
            "low": float(cur["low"]),
            "close": float(cur["close"]),
            "volume": float(cur.get("volume", 0.0)),
            "source": "STREAM",
            "open_time": o,
            "close_time": c,
            "age_sec": _age_sec(prev_ts),
        }
        _append_stream_closed(closed)

    # start new forming candle
    STATE.stream_current = {
        "symbol": STATE.symbol,
        "timestamp": ts_open,
        "open": float(price),
        "high": float(price),
        "low": float(price),
        "close": float(price),
        "volume": 1.0,
        "source": "STREAM_FORMING",
        "open_time": _open_close_str(ts_open)[0],
        "close_time": _open_close_str(ts_open)[1],
    }


def _extract_ticks_from_any(payload: Any) -> List[tuple[float, float]]:
    """
    Try to extract ticks in shape:
    [[symbol, ts, price], ...]
    """
    out: List[tuple[float, float]] = []

    data = payload
    if isinstance(payload, dict):
        # some internal handlers pass dict
        for k in ("data", "payload", "ticks"):
            if k in payload:
                data = payload[k]
                break

    if isinstance(data, list):
        for it in data:
            if isinstance(it, list) and len(it) >= 3 and str(it[0]) == STATE.symbol:
                try:
                    out.append((float(it[1]), float(it[2])))
                except Exception:
                    pass

    return out


def _try_parse_socketio_message(msg: Any) -> Optional[Any]:
    """
    Pocket websocket_client may pass raw strings like:
    42["updateStream",[[...]]]
    42["changeSymbol",{"asset":"..."}]
    We parse only "42[...]".
    """
    if not isinstance(msg, str):
        return None
    if not msg.startswith("42"):
        return None
    raw = msg[2:]
    try:
        arr = json.loads(raw)
        # arr should be [eventName, payload]
        if isinstance(arr, list) and len(arr) >= 2:
            return arr[1]
    except Exception:
        return None
    return None


def _ws_raw_add(msg: Any) -> None:
    """
    Store last raw websocket messages (str/bytes/anything) for debugging.
    Keeps only last 30 items.
    """
    try:
        STATE.socket_raw_total = int(STATE.socket_raw_total or 0) + 1
        STATE.socket_raw_last_ts = time.time()

        preview = ""
        msg_type = type(msg).__name__

        if isinstance(msg, bytes):
            try:
                s = msg.decode("utf-8", errors="replace")
                preview = s[:300]
            except Exception:
                preview = msg[:80].hex()
        elif isinstance(msg, str):
            preview = msg[:300]
        else:
            preview = str(msg)[:300]

        item = {
            "ts": STATE.socket_raw_last_ts,
            "type": msg_type,
            "preview": preview,
        }

        tail = STATE.socket_raw_tail or []
        tail.append(item)
        if len(tail) > 30:
            tail = tail[-30:]
        STATE.socket_raw_tail = tail
    except Exception:
        pass


def _patch_method(obj: Any, name: str, handler: Callable[[Any], None]) -> None:
    orig = getattr(obj, name, None)
    if not callable(orig):
        return

    def wrapped(data: Any):
        try:
            handler(data)
        except Exception:
            pass
        try:
            return orig(data)
        except Exception:
            return None

    try:
        setattr(obj, name, wrapped)
    except Exception:
        pass


def _install_stream_hooks(client: Any, loop: asyncio.AbstractEventLoop) -> None:
    """
    Install hooks to catch tick stream.
    """
    async def apply_ticks(payload: Any):
        ticks = _extract_ticks_from_any(payload)
        if not ticks:
            return
        async with _lock:
            for t, p in ticks:
                _on_tick(t, p)
            _update_hybrid_choice()

    def handler(payload: Any):
        loop.call_soon_threadsafe(asyncio.create_task, apply_ticks(payload))

    # hook internal handlers on client
    for m in ("_handle_candles_stream", "_on_stream_update", "_on_json_data"):
        _patch_method(client, m, handler)

    # hook websocket_client._process_message to:
    # 1) store every raw message to tail
    # 2) try parse socketio "42[...]"
    ws = getattr(client, "websocket_client", None) or getattr(client, "_websocket_client", None)
    if ws:
        def ws_handler(raw_msg: Any):
            _ws_raw_add(raw_msg)

            # normalize bytes -> str for parser (if possible)
            msg_for_parse = raw_msg
            if isinstance(raw_msg, bytes):
                try:
                    msg_for_parse = raw_msg.decode("utf-8", errors="ignore")
                except Exception:
                    msg_for_parse = None

            if msg_for_parse is not None:
                payload = _try_parse_socketio_message(msg_for_parse)
                if payload is not None:
                    handler(payload)

        _patch_method(ws, "_process_message", ws_handler)


async def _fetch_history_closed(client: Any, limit: int = 120) -> List[Dict[str, Any]]:
    fn = getattr(client, "get_candles", None)
    if not callable(fn):
        return []

    res = fn(STATE.symbol, TF_SEC, count=limit)
    if inspect.isawaitable(res):
        res = await res
    if not res:
        return []

    candles: List[Dict[str, Any]] = []
    for x in res:
        dt = getattr(x, "timestamp", None)
        if not isinstance(dt, datetime.datetime):
            continue

        # dt.timestamp() appears shifted in your environment -> subtract forced offset
        ts_open = _align(int(dt.timestamp()) - int(STATE.po_time_offset_sec))
        o, c = _open_close_str(ts_open)

        candles.append({
            "symbol": STATE.symbol,
            "timestamp": ts_open,
            "open": float(x.open),
            "high": float(x.high),
            "low": float(x.low),
            "close": float(x.close),
            "volume": float(getattr(x, "volume", 0.0)),
            "source": "HISTORY",
            "open_time": o,
            "close_time": c,
        })

    candles.sort(key=lambda cc: cc["timestamp"])
    # closed are all except last
    closed = candles[:-1] if len(candles) >= 2 else []
    for cc in closed:
        cc["age_sec"] = _age_sec(int(cc["timestamp"]))
    return closed[-200:]


async def _engine_loop():
    STATE.last_connect_error = "—"
    STATE.last_candles_error = "—"
    STATE.last_fetch_count = 0
    STATE.last_poll_ts = None

    STATE.stream_current = None
    STATE.stream_closed = []
    STATE.stream = StreamStats()
    STATE.active_mode = "HISTORY"
    STATE.history_age_sec = None
    STATE.stream_age_sec = None

    STATE.socket_raw_tail = []
    STATE.socket_raw_total = 0
    STATE.socket_raw_last_ts = None

    ssid = _read_ssid()
    if not ssid:
        STATE.connected = False
        STATE.last_connect_error = "Missing SSID"
        return

    try:
        client = AsyncPocketOptionClient(ssid=ssid, is_demo=STATE.is_demo, enable_logging=False)
        await client.connect()
        STATE.connected = True
    except Exception as e:
        STATE.connected = False
        STATE.last_connect_error = f"{type(e).__name__}: {e}"
        return

    loop = asyncio.get_running_loop()
    _install_stream_hooks(client, loop)

    while not _stop_event.is_set():
        STATE.last_poll_ts = time.time()
        try:
            hist_closed = await _fetch_history_closed(client, limit=120)
            async with _lock:
                STATE.history_closed = hist_closed
                STATE.last_fetch_count = len(hist_closed)
                _update_hybrid_choice()
            STATE.last_candles_error = "—"
        except Exception as e:
            STATE.last_candles_error = f"{type(e).__name__}: {e}"
        await asyncio.sleep(2.0)

    STATE.connected = False


@app.get("/api/status")
def api_status():
    d = asdict(STATE)
    d["stream"] = asdict(STATE.stream)
    return d


@app.get("/api/ws_tail")
def api_ws_tail():
    return {
        "ok": True,
        "socket_raw_total": STATE.socket_raw_total,
        "socket_raw_last_ts": STATE.socket_raw_last_ts,
        "socket_raw_last_age_sec": (time.time() - STATE.socket_raw_last_ts) if STATE.socket_raw_last_ts else None,
        "tail": STATE.socket_raw_tail or [],
    }


@app.post("/api/start")
async def api_start():
    global _engine_task
    if STATE.running and _engine_task and not _engine_task.done():
        return {"ok": True, "msg": "already running"}
    _stop_event.clear()
    STATE.running = True
    _engine_task = asyncio.create_task(_engine_loop())
    return {"ok": True, "msg": "started"}


@app.post("/api/stop")
async def api_stop():
    global _engine_task
    if not STATE.running:
        return {"ok": True, "msg": "already stopped"}
    _stop_event.set()
    STATE.running = False
    if _engine_task:
        try:
            await asyncio.wait_for(_engine_task, timeout=3.0)
        except Exception:
            pass
    return {"ok": True, "msg": "stopped"}


@app.get("/api/compare_last_closed")
def api_compare_last_closed():
    active = _active_last_closed()
    hist = _pick_last_history()
    stream = _pick_last_stream_closed()

    return {
        "ok": True,
        "now_local": _fmt(_now_ts()),
        "symbol": STATE.symbol,
        "po_time_offset_sec": STATE.po_time_offset_sec,
        "fresh_max_sec": STATE.fresh_max_sec,
        "active_mode": STATE.active_mode,
        "history_last": hist,
        "stream_last": stream,
        "active": active,
        "stream_ticks_seen": STATE.stream.ticks_seen,
        "last_tick_local": STATE.stream.last_tick_local,
        "last_tick_po_ts": STATE.stream.last_tick_po_ts,
        "last_tick_price": STATE.stream.last_tick_price,
        "history_age_sec": STATE.history_age_sec,
        "stream_age_sec": STATE.stream_age_sec,
        "stream_closed_len": len(STATE.stream_closed or []),
        "stream_current_is_null": (STATE.stream_current is None),
    }


@app.get("/")
def root():
    return {
        "app": APP_NAME,
        "endpoints": [
            "/api/status",
            "/api/ws_tail",
            "/api/start",
            "/api/stop",
            "/api/compare_last_closed",
        ],
        "symbol": STATE.symbol,
        "tf_sec": STATE.tf_sec,
        "po_time_offset_sec": STATE.po_time_offset_sec,
        "fresh_max_sec": STATE.fresh_max_sec,
    }
