import os\
import time\
import asyncio\
import inspect\
from datetime import datetime\
from typing import Any, Dict, List, Optional, Tuple\
\
from fastapi import FastAPI\
from fastapi.responses import HTMLResponse, JSONResponse\
\
from pocketoptionapi_async.client import AsyncPocketOptionClient\
\
APP_TITLE = "Money Printer   Pocket"\
DEFAULT_SYMBOL = os.getenv("PO_SYMBOL", "CADCHF_otc")\
DEFAULT_IS_DEMO = os.getenv("PO_IS_DEMO", "1") in ("1", "true", "True", "yes", "YES")\
\
\
def _read_ssid() -> str:\
    ssid = os.getenv("PO_SSID", "").strip()\
    if ssid:\
        return ssid\
    path = os.path.join(os.getcwd(), "po_ssid.txt")\
    if os.path.exists(path):\
        return open(path, "r", encoding="utf-8").read().strip()\
    return ""\
\
\
def _now_ts() -> int:\
    return int(time.time())\
\
\
def _to_float(x: Any) -> Optional[float]:\
    try:\
        return float(x)\
    except Exception:\
        return None\
\
\
def _to_ts(x: Any) -> Optional[int]:\
    if x is None:\
        return None\
    if isinstance(x, datetime):\
        return int(x.timestamp())\
    try:\
        v = int(float(x))\
    except Exception:\
        return None\
    # PocketOption obăas vrací timestamp v milisekundÃch\
    if v > 10**12:\
        v //= 1000\
    return v\
\
\
def _normalize_candle(c: Any) -> Optional[Dict[str, Any]]:\
    if c is None:\
        return None\
\
    if hasattr(c, "open") and hasattr(c, "high") and hasattr(c, "low") and hasattr(c, "close"):\
        ts_raw = getattr(c, "timestamp", None) or getattr(c, "time", None) or getattr(c, "ts", None)\
        ts = _to_ts(ts_raw)\
        if ts is None:\
            return None\
        o = _to_float(getattr(c, "open", None))\
        h = _to_float(getattr(c, "high", None))\
        l = _to_float(getattr(c, "low", None))\
        cl = _to_float(getattr(c, "close", None))\
        if None in (o, h, l, cl):\
            return None\
        return {"ts": ts, "open": o, "high": h, "low": l, "close": cl}\
\
    if isinstance(c, (list, tuple)) and len(c) >= 5:\
        ts = _to_ts(c[0])\
        o = _to_float(c[1])\
        cl = _to_float(c[2])\
        h = _to_float(c[3])\
        l = _to_float(c[4])\
        if ts is None or None in (o, h, l, cl):\
            return None\
        return {"ts": ts, "open": o, "high": h, "low": l, "close": cl}\
\
    if isinstance(c, dict):\
        ts_raw = c.get("time") or c.get("timestamp") or c.get("t") or c.get("from") or c.get("ts")\
        ts = _to_ts(ts_raw)\
        if ts is None:\
            return None\
        o = _to_float(c.get("open") or c.get("o"))\
        h = _to_float(c.get("high") or c.get("h"))\
        l = _to_float(c.get("low") or c.get("l"))\
        cl = _to_float(c.get("close") or c.get("c"))\
        if None in (o, h, l, cl):\
            return None\
        return {"ts": ts, "open": o, "high": h, "low": l, "close": cl}\
\
    return None\
\
\
def _has_upper_wick(o: float, c: float, h: float, eps: float = 1e-12) -> bool:\
    return h > max(o, c) + eps\
\
\
def _has_lower_wick(o: float, c: float, l: float, eps: float = 1e-12) -> bool:\
    return l < min(o, c) - eps\
\
\
def _is_cross(cndl: Dict[str, Any]) -> bool:\
    o, h, l, c = cndl["open"], cndl["high"], cndl["low"], cndl["close"]\
    return _has_upper_wick(o, c, h) and _has_lower_wick(o, c, l)\
\
\
def _safe_extract_candles(res: Any) -> List[Dict[str, Any]]:\
    if not res:\
        return []\
\
    if isinstance(res, (tuple, list)) and res and isinstance(res[0], list):\
        res = res[0]\
\
    if isinstance(res, dict):\
        for k in ("candles", "data", "items", "result"):\
            if k in res and isinstance(res[k], list):\
                res = res[k]\
                break\
\
    if not isinstance(res, list):\
        return []\
\
    out: List[Dict[str, Any]] = []\
    for item in res:\
        nc = _normalize_candle(item)\
        if nc:\
            out.append(nc)\
    out.sort(key=lambda x: x["ts"])\
    return out\
\
\
async def _maybe_await(x: Any) -> Any:\
    return await x if inspect.isawaitable(x) else x\
\
\
def _parse_balance(value: Any) -> Optional[float]:\
    if value is None:\
        return None\
    if isinstance(value, (int, float)):\
        return float(value)\
    if isinstance(value, dict):\
        for k in ("balance", "amount", "value", "available", "total"):\
            if k in value:\
                v = _to_float(value[k])\
                if v is not None:\
                    return v\
        return None\
    for dump_name in ("model_dump", "dict", "to_dict", "as_dict"):\
        if hasattr(value, dump_name):\
            try:\
                d = getattr(value, dump_name)()\
                got = _parse_balance(d)\
                if got is not None:\
                    return got\
            except Exception:\
                pass\
    for attr in ("balance", "amount", "value", "available", "total"):\
        if hasattr(value, attr):\
            try:\
                got = _parse_balance(getattr(value, attr))\
                if got is not None:\
                    return got\
            except Exception:\
                pass\
    try:\
        got = _parse_balance(vars(value))\
        if got is not None:\
            return got\
    except Exception:\
        pass\
    return None\
\
\
class Engine:\
    def __init__(self) -> None:\
        self.running = False\
        self.connected = False\
        self.symbol = DEFAULT_SYMBOL\
        self.is_demo = DEFAULT_IS_DEMO\
\
        self.balance: Optional[float] = None\
        self.balance_source: str = " "\
\
        self.last_poll_ts: Optional[int] = None\
        self.last_fetch_count: int = 0\
        self.last_candles_error: str = " "\
\
        # DEBUG: rozsah timestampů z API\
        self.fetched_min_ts: Optional[int] = None\
        self.fetched_max_ts: Optional[int] = None\
        self.fetched_age_sec: Optional[int] = None  # now - max_ts\
\
        self.candles_by_ts: Dict[int, Dict[str, Any]] = {}\
        self.candles: List[Dict[str, Any]] = []\
\
        self._lock = asyncio.Lock()\
        self._stop = asyncio.Event()\
        self._task: Optional[asyncio.Task] = None\
        self._client: Optional[AsyncPocketOptionClient] = None\
\
    async def start(self) -> Tuple[bool, str]:\
        async with self._lock:\
            if self.running:\
                return True, "already running"\
\
            ssid = _read_ssid()\
            if not ssid:\
                return False, "Chybí SSID: dej do po_ssid.txt (v ~/bot-srdce) nebo do env PO_SSID"\
\
            try:\
                self._client = AsyncPocketOptionClient(ssid, is_demo=self.is_demo, enable_logging=False)\
                await self._client.connect()\
                self.connected = True\
                self.running = True\
                self._stop.clear()\
\
                self._task = asyncio.create_task(self._loop())\
                return True, "started"\
            except Exception as e:\
                self.connected = False\
                self.running = False\
                return False, f"start/connect error: {e}"\
\
    async def stop(self) -> Tuple[bool, str]:\
        async with self._lock:\
            if not self.running:\
                return True, "already stopped"\
\
            self._stop.set()\
            self.running = False\
\
            try:\
                if self._task:\
                    try:\
                        await asyncio.wait_for(self._task, timeout=5)\
                    except Exception:\
                        pass\
                if self._client:\
                    try:\
                        await self._client.disconnect()\
                    except Exception:\
                        pass\
            finally:\
                self.connected = False\
                self._task = None\
                self._client = None\
\
            return True, "stopped"\
\
    async def set_symbol(self, symbol: str) -> Tuple[bool, str]:\
        symbol = (symbol or "").strip()\
        if not symbol:\
            return False, "empty symbol"\
        async with self._lock:\
            self.symbol = symbol\
            self.candles_by_ts = {}\
            self.candles = []\
            self.fetched_min_ts = None\
            self.fetched_max_ts = None\
            self.fetched_age_sec = None\
            return True, "symbol set"\
\
    async def _fetch_candles(self, symbol: str, count: int = 150) -> List[Dict[str, Any]]:\
        self.last_candles_error = " "\
        if not self._client:\
            return []\
\
        fn = self._client.get_candles\
        try:\
            res = await fn(asset=symbol, timeframe=60, count=count)\
            return _safe_extract_candles(res)\
        except TypeError:\
            try:\
                res = await fn(symbol, 60, count)\
                return _safe_extract_candles(res)\
            except Exception as e2:\
                self.last_candles_error = str(e2)\
                raise\
        except Exception as e:\
            self.last_candles_error = str(e)\
            raise\
\
    async def _update_balance(self) -> None:\
        if not self._client or not hasattr(self._client, "get_balance"):\
            return\
        raw = await _maybe_await(self._client.get_balance())\
        bal = _parse_balance(raw)\
        if bal is not None:\
            self.balance = bal\
            self.balance_source = f"get_balance():{type(raw).__name__}"\
\
    def _mk_row(self, c: Dict[str, Any], now_ts: int) -> Dict[str, Any]:\
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]\
        cross = _is_cross(c)\
        upper = _has_upper_wick(o, cl, h)\
        lower = _has_lower_wick(o, cl, l)\
\
        # M1 logika: svíăka s ts je "LIVE" dokud now < ts+60\
        status = "LIVE" if now_ts < (c["ts"] + 60) else "UZAVŃENÁ"\
\
        return {\
            "ts": c["ts"],\
            "symbol": self.symbol,\
            "status": status,\
            "open": o,\
            "high": h,\
            "low": l,\
            "close": cl,\
            "upper": upper,\
            "lower": lower,\
            "is_cross": cross,\
        }\
\
    def _merge_candles(self, fetched: List[Dict[str, Any]], now_ts: int) -> int:\
        changed = 0\
        for c in fetched:\
            ts = c["ts"]\
            row = self._mk_row(c, now_ts)\
            prev = self.candles_by_ts.get(ts)\
            if prev != row:\
                self.candles_by_ts[ts] = row\
                changed += 1\
\
        keys = sorted(self.candles_by_ts.keys())[-800:]\
        self.candles_by_ts = {k: self.candles_by_ts[k] for k in keys}\
        self.candles = [self.candles_by_ts[k] for k in keys]\
        return changed\
\
    async def _loop(self) -> None:\
        assert self._client is not None\
\
        while not self._stop.is_set():\
            try:\
                now = _now_ts()\
                self.last_poll_ts = now\
\
                try:\
                    await self._update_balance()\
                except Exception:\
                    pass\
\
                fetched = await self._fetch_candles(self.symbol, count=150)\
                self.last_fetch_count = len(fetched)\
\
                if fetched:\
                    self.fetched_min_ts = fetched[0]["ts"]\
                    self.fetched_max_ts = fetched[-1]["ts"]\
                    self.fetched_age_sec = max(0, now - self.fetched_max_ts)\
                else:\
                    self.fetched_min_ts = None\
                    self.fetched_max_ts = None\
                    self.fetched_age_sec = None\
\
                self._merge_candles(fetched, now)\
\
                self.connected = True\
            except Exception:\
                self.connected = False\
\
            await asyncio.sleep(0.8)\
\
\
engine = Engine()\
app = FastAPI(title=APP_TITLE)\
\
\
@app.get("/", response_class=HTMLResponse)\
def home() -> str:\
    return """\
<!doctype html>\
<html lang="cs">\
<head>\
  <meta charset="utf-8"/>\
  <meta name="viewport" content="width=device-width,initial-scale=1"/>\
  <title>Money Printer</title>\
  <style>\
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:18px;background:#0b1220;color:#e8eefc;}\
    .wrap{max-width:980px;margin:0 auto;}\
    .card{background:#111a2e;border:1px solid #1e2a49;border-radius:16px;padding:14px;margin-bottom:12px;}\
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}\
    button{border:0;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;}\
    .g{background:#22c55e;color:#05210f;}\
    .r{background:#ef4444;color:#2a0606;}\
    .b{background:#60a5fa;color:#071427;}\
    input{padding:10px 12px;border-radius:12px;border:1px solid #2a3a63;background:#0b1220;color:#e8eefc;}\
    .pill{display:inline-block;padding:6px 10px;border-radius:999px;font-weight:900;}\
    .ok{background:#16a34a22;border:1px solid #16a34a55;color:#86efac;}\
    .bad{background:#ef444422;border:1px solid #ef444455;color:#fecaca;}\
    .muted{color:#9db1df;}\
    .bigbal{font-size:34px;font-weight:1000;letter-spacing:0.5px}\
    table{width:100%;border-collapse:collapse;font-size:14px;}\
    th,td{border-bottom:1px solid #1e2a49;padding:8px 6px;text-align:left;vertical-align:top;}\
    code{background:#0b1220;border:1px solid #1e2a49;padding:2px 6px;border-radius:8px;}\
    .tag{display:inline-block;padding:4px 10px;border-radius:999px;font-weight:1000;}\
    .tagCross{background:#16a34a22;border:1px solid #16a34a55;color:#86efac;}\
    .tagNorm{background:#94a3b822;border:1px solid #94a3b855;color:#cbd5e1;}\
    .tagLive{background:#f59e0b22;border:1px solid #f59e0b55;color:#fde68a;}\
  </style>\
</head>\
<body>\
<div class="wrap">\
  <h2 style="margin:0 0 10px 0;">Money Printer   Pocket</h2>\
\
  <div class="card">\
    <div class="row">\
      <button class="g" onclick="apiStart()">START</button>\
      <button class="r" onclick="apiStop()">STOP</button>\
\
      <input id="symbol" list="symbols" placeholder="symbol (napŃ. CADCHF_otc)" style="min-width:220px;">\
      <datalist id="symbols">\
        <option value="CADCHF_otc"></option>\
        <option value="GBPJPY_otc"></option>\
        <option value="GBPAUD_otc"></option>\
        <option value="AUDCAD_otc"></option>\
        <option value="AEDCNY_otc"></option>\
        <option value="EURGBP_otc"></option>\
      </datalist>\
      <button class="b" onclick="setSymbol()">SET SYMBOL</button>\
    </div>\
\
    <div style="margin-top:12px" class="row">\
      <div>Stav: <span id="run" class="pill bad"> </span></div>\
      <div>PŃipojení: <span id="conn" class="pill bad"> </span></div>\
      <div>Symbol: <code id="sym"> </code></div>\
      <div class="muted">Data: <code id="fc">0</code> svíăek z API</div>\
      <div class="muted">max_ts: <code id="mx"> </code></div>\
      <div class="muted">age_sec: <code id="age"> </code></div>\
      <div class="muted">Chyba candles: <code id="ce"> </code></div>\
      <div class="muted">ZapsÃno: <code id="cnt">0</code></div>\
    </div>\
\
    <div style="margin-top:10px">\
      <div class="muted">Balance</div>\
      <div class="bigbal"><span id="bal"> </span> <span class="muted" id="balsrc" style="font-size:12px"></span></div>\
    </div>\
  </div>\
\
  <div class="card">\
    <h3 style="margin:0 0 8px 0;">Svíăky (M1)   OHLC + kŃížek</h3>\
    <table>\
      <thead>\
        <tr>\
          <th>ăas</th><th>symbol</th><th>stav</th><th>typ</th>\
          <th>O</th><th>H</th><th>L</th><th>C</th><th>knoty</th>\
        </tr>\
      </thead>\
      <tbody id="candles"></tbody>\
    </table>\
  </div>\
</div>\
\
<script>\
async function jget(url){ const r = await fetch(url); return await r.json(); }\
async function jpost(url, body){\
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body||{})});\
  return await r.json();\
}\
function pill(el, ok, text){\
  el.textContent = text;\
  el.className = "pill " + (ok ? "ok" : "bad");\
}\
function tagType(c){\
  return c.is_cross\
    ? '<span class="tag tagCross">KŃÃŽEK</span>'\
    : '<span class="tag tagNorm">SVÃăKA</span>';\
}\
function tagStatus(c){\
  return c.status === "LIVE"\
    ? '<span class="tag tagLive">LIVE</span>'\
    : '<span class="tag tagNorm">UZAVŃENÁ</span>';\
}\
function fmtLocal(ts){\
  if(!ts) return " ";\
  return new Date(ts*1000).toLocaleTimeString(); // tvoje lokalni zona\
}\
async function refresh(){\
  const st = await jget("/api/status");\
  pill(document.getElementById("run"), st.running, st.running ? "BăŽÃ" : "STOP");\
  pill(document.getElementById("conn"), st.connected, st.connected ? "OK" : "NE");\
  document.getElementById("sym").textContent = st.symbol || " ";\
  document.getElementById("fc").textContent = String(st.last_fetch_count || 0);\
  document.getElementById("ce").textContent = st.last_candles_error || " ";\
  document.getElementById("mx").textContent = st.fetched_max_ts ? fmtLocal(st.fetched_max_ts) : " ";\
  document.getElementById("age").textContent = (st.fetched_age_sec==null ? " " : String(st.fetched_age_sec));\
\
  document.getElementById("bal").textContent = (st.balance==null ? " " : st.balance.toFixed(2));\
  document.getElementById("balsrc").textContent = st.balance_source ? ("(" + st.balance_source + ")") : "";\
\
  const list = st.candles || [];\
  document.getElementById("cnt").textContent = String(list.length);\
\
  const tb = document.getElementById("candles");\
  tb.innerHTML = "";\
  list.slice().reverse().forEach(x=>{\
    const w = `${x.upper ? "U" : "-"} ${x.lower ? "L" : "-"}`;\
    const tr = document.createElement("tr");\
    tr.innerHTML = `\
      <td><code>${fmtLocal(x.ts)}</code></td>\
      <td>${x.symbol}</td>\
      <td>${tagStatus(x)}</td>\
      <td>${tagType(x)}</td>\
      <td>${x.open}</td><td>${x.high}</td><td>${x.low}</td><td>${x.close}</td>\
      <td class="muted">${w}</td>\
    `;\
    tb.appendChild(tr);\
  });\
}\
async function apiStart(){ await jpost("/api/start"); await refresh(); }\
async function apiStop(){ await jpost("/api/stop"); await refresh(); }\
async function setSymbol(){\
  const s = document.getElementById("symbol").value.trim();\
  if(!s) return;\
  await jpost("/api/symbol", {symbol:s});\
  await refresh();\
}\
setInterval(refresh, 1000);\
refresh();\
</script>\
</body>\
</html>\
"""\
\
\
@app.post("/api/start")\
async def api_start():\
    ok, msg = await engine.start()\
    return JSONResponse({"ok": ok, "msg": msg})\
\
\
@app.post("/api/stop")\
async def api_stop():\
    ok, msg = await engine.stop()\
    return JSONResponse({"ok": ok, "msg": msg})\
\
\
@app.post("/api/symbol")\
async def api_symbol(payload: Dict[str, Any]):\
    ok, msg = await engine.set_symbol(str(payload.get("symbol", "")))\
    return JSONResponse({"ok": ok, "msg": msg, "symbol": engine.symbol})\
\
\
@app.get("/api/status")\
def api_status():\
    return JSONResponse({\
        "running": engine.running,\
        "connected": engine.connected,\
        "symbol": engine.symbol,\
        "is_demo": engine.is_demo,\
        "balance": engine.balance,\
        "balance_source": engine.balance_source,\
        "last_poll_ts": engine.last_poll_ts,\
        "last_fetch_count": engine.last_fetch_count,\
        "last_candles_error": engine.last_candles_error,\
        "fetched_min_ts": engine.fetched_min_ts,\
        "fetched_max_ts": engine.fetched_max_ts,\
        "fetched_age_sec": engine.fetched_age_sec,\
        "candles": engine.candles,\
    })\
PY\

cd ~/bot-srdce\
source .venv/bin/activate\
export PO_SSID="$(cat po_ssid.txt)"\
export PO_IS_DEMO="1"\
uvicorn app.main:app --reload\

pwd\
ls -la\
\
export PO_SSID='SEM_VLOZ_TVŮJ_SSID'\
export SYMBOL='CADCHF_otc'\
export BOT_TZ='local'          # nebo tŃeba: export BOT_TZ='Europe/Prague'\
python3 minute_candles.py\

set +H\

cd /Users/macbook/bot-srdce\
\
source .venv/bin/activate\
\
export PO_SSID="$(cat po_ssid.txt)"\
export SYMBOL="CADCHF_otc"\
export BOT_TZ="local"\
\
python3 minute_candles.py\

cd /Users/macbook/bot-srdce\
\
cat > minute_candles.py <<'PY'\
#!/usr/bin/env python3\
# -*- coding: utf-8 -*-\
\
import os\
import time\
import csv\
import signal\
from dataclasses import dataclass\
from datetime import datetime\
from typing import Any, Dict, List, Optional, Tuple\
\
try:\
    from zoneinfo import ZoneInfo\
except Exception:\
    ZoneInfo = None  # type: ignore\
\
SYMBOL = os.getenv("SYMBOL", "CADCHF_otc")\
TIMEFRAME_SEC = int(os.getenv("TIMEFRAME_SEC", "60"))\
FETCH_LAST_N = int(os.getenv("FETCH_LAST_N", "120"))\
POLL_SEC = float(os.getenv("POLL_SEC", "1.0"))\
OUT_CSV = os.getenv("OUT_CSV", f"candles_{SYMBOL}.csv")\
BOT_TZ = os.getenv("BOT_TZ", "local")\
\
def _get_tzinfo():\
    if BOT_TZ.lower() == "local":\
        return datetime.now().astimezone().tzinfo\
    if ZoneInfo is None:\
        return datetime.now().astimezone().tzinfo\
    try:\
        return ZoneInfo(BOT_TZ)\
    except Exception:\
        return datetime.now().astimezone().tzinfo\
\
TZINFO = _get_tzinfo()\
\
def fmt_ts(ts_sec: int) -> str:\
    return datetime.fromtimestamp(ts_sec, tz=TZINFO).strftime("%H:%M:%S")\
\
def safe_int(x: Any, default: int = 0) -> int:\
    try:\
        return int(float(x))\
    except Exception:\
        return default\
\
def extract_ts_sec(c: Dict[str, Any]) -> Optional[int]:\
    for k in ("time", "t", "timestamp", "time_stamp"):\
        if k in c and c[k] is not None:\
            ts = safe_int(c[k], default=-1)\
            if ts <= 0:\
                continue\
            if ts > 10_000_000_000:\
                ts //= 1000\
            return ts\
    return None\
\
def extract_ohlc(c: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:\
    def gf(*keys: str) -> Optional[float]:\
        for k in keys:\
            if k in c and c[k] is not None:\
                try:\
                    return float(c[k])\
                except Exception:\
                    pass\
        return None\
\
    o = gf("open", "o")\
    h = gf("high", "h")\
    l = gf("low", "l")\
    cl = gf("close", "c")\
\
    if o is None or h is None or l is None or cl is None:\
        return None\
    return (o, h, l, cl)\
\
@dataclass\
class Candle:\
    ts: int\
    o: float\
    h: float\
    l: float\
    c: float\
\
def ensure_csv_header(path: str) -> None:\
    if os.path.exists(path) and os.path.getsize(path) > 0:\
        return\
    with open(path, "w", newline="", encoding="utf-8") as f:\
        w = csv.writer(f)\
        w.writerow(["time_local", "ts", "symbol", "O", "H", "L", "C"])\
\
def append_candle_csv(path: str, symbol: str, cd: Candle) -> None:\
    with open(path, "a", newline="", encoding="utf-8") as f:\
        w = csv.writer(f)\
        w.writerow([fmt_ts(cd.ts), cd.ts, symbol, cd.o, cd.h, cd.l, cd.c])\
\
def die(msg: str, code: int = 1) -> None:\
    print(f"[ERR] {msg}")\
    raise SystemExit(code)\
\
def make_client():\
    try:\
        from pocketoptionapi.stable_api import PocketOption  # type: ignore\
    except Exception as e:\
        die(f"Nenalezen pocketoptionapi.stable_api PocketOption. Chyba: {e}")\
\
    ssid = os.getenv("PO_SSID", "").strip()\
    if not ssid:\
        die("Chybí PO_SSID. Dej: export PO_SSID='...'\nPak znovu spusť script.")\
    return PocketOption(ssid)\
\
def connect(client) -> None:\
    ok = False\
    for fn_name in ("connect", "check_connect"):\
        fn = getattr(client, fn_name, None)\
        if callable(fn):\
            try:\
                res = fn()\
                ok = bool(res[0]) if isinstance(res, tuple) else bool(res)\
                if ok:\
                    break\
            except Exception:\
                pass\
    if not ok:\
        die("NepodaŃilo se pŃipojit k PocketOption (connect/check_connect selhalo).")\
\
def fetch_candles(client, symbol: str, timeframe_sec: int, count: int) -> List[Dict[str, Any]]:\
    now = int(time.time())\
    candidates = [\
        (symbol, timeframe_sec, count, now),\
        (symbol, timeframe_sec, now, count),\
        (symbol, timeframe_sec, count),\
        (symbol, timeframe_sec),\
    ]\
    last_exc = None\
    for args in candidates:\
        try:\
            res = client.get_candles(*args)  # type: ignore\
            if isinstance(res, list):\
                return res\
            if isinstance(res, dict):\
                for k in ("candles", "data", "result"):\
                    if k in res and isinstance(res[k], list):\
                        return res[k]\
        except Exception as e:\
            last_exc = e\
    if last_exc:\
        raise last_exc\
    return []\
\
RUNNING = True\
\
def _sig_handler(_sig, _frame):\
    global RUNNING\
    RUNNING = False\
\
def main():\
    global RUNNING\
    signal.signal(signal.SIGINT, _sig_handler)\
    signal.signal(signal.SIGTERM, _sig_handler)\
\
    ensure_csv_header(OUT_CSV)\
\
    client = make_client()\
    connect(client)\
\
    print(f"[OK] CONNECTED | symbol={SYMBOL} | tf={TIMEFRAME_SEC}s | tz={BOT_TZ} | csv={OUT_CSV}")\
\
    last_saved_ts = 0\
\
    try:\
        raw = fetch_candles(client, SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N)\
        parsed: List[Candle] = []\
        for c in raw:\
            if not isinstance(c, dict):\
                continue\
            ts = extract_ts_sec(c)\
            ohlc = extract_ohlc(c)\
            if ts is None or ohlc is None:\
                continue\
            o, h, l, cl = ohlc\
            parsed.append(Candle(ts, o, h, l, cl))\
        parsed.sort(key=lambda x: x.ts)\
        if parsed:\
            last_saved_ts = parsed[-1].ts\
            for cd in parsed[-3:]:\
                append_candle_csv(OUT_CSV, SYMBOL, cd)\
            print(f"[OK] WARMUP last_ts={last_saved_ts} ({fmt_ts(last_saved_ts)})")\
        else:\
            print("[WARN] WARMUP: žÃdnÃ data (prÃzdný seznam svíăek).")\
    except Exception as e:\
        print(f"[WARN] Warmup selhal: {e}")\
\
    while RUNNING:\
        try:\
            raw = fetch_candles(client, SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N)\
            candles: List[Candle] = []\
            for c in raw:\
                if not isinstance(c, dict):\
                    continue\
                ts = extract_ts_sec(c)\
                ohlc = extract_ohlc(c)\
                if ts is None or ohlc is None:\
                    continue\
                o, h, l, cl = ohlc\
                candles.append(Candle(ts, o, h, l, cl))\
\
            candles.sort(key=lambda x: x.ts)\
\
            new_ones = [cd for cd in candles if cd.ts > last_saved_ts]\
            for cd in new_ones:\
                append_candle_csv(OUT_CSV, SYMBOL, cd)\
                print(f"{fmt_ts(cd.ts)}  {SYMBOL}  O={cd.o:.5f} H={cd.h:.5f} L={cd.l:.5f} C={cd.c:.5f}")\
                last_saved_ts = cd.ts\
\
        except Exception as e:\
            print(f"[WARN] fetch/parse chyba: {e}")\
\
        time.sleep(POLL_SEC)\
\
    print("[OK] STOP")\
\
if __name__ == "__main__":\
    main()\
PY\
\
chmod +x minute_candles.py\
ls -la minute_candles.py\
\
source .venv/bin/activate\
\
export PO_SSID="$(cat po_ssid.txt)"\
export SYMBOL="CADCHF_otc"\
export BOT_TZ="local"\
\
python3 minute_candles.py\

cd /Users/macbook/bot-srdce\
\
cat > minute_candles.py <<'PY'\
#!/usr/bin/env python3\
# -*- coding: utf-8 -*-\
\
import os\
import time\
import csv\
import signal\
from dataclasses import dataclass\
from datetime import datetime\
from typing import Any, Dict, List, Optional, Tuple\
\
try:\
    from zoneinfo import ZoneInfo\
except Exception:\
    ZoneInfo = None  # type: ignore\
\
SYMBOL = os.getenv("SYMBOL", "CADCHF_otc")\
TIMEFRAME_SEC = int(os.getenv("TIMEFRAME_SEC", "60"))\
FETCH_LAST_N = int(os.getenv("FETCH_LAST_N", "120"))\
POLL_SEC = float(os.getenv("POLL_SEC", "1.0"))\
OUT_CSV = os.getenv("OUT_CSV", f"candles_{SYMBOL}.csv")\
BOT_TZ = os.getenv("BOT_TZ", "local")\
\
def _get_tzinfo():\
    if BOT_TZ.lower() == "local":\
        return datetime.now().astimezone().tzinfo\
    if ZoneInfo is None:\
        return datetime.now().astimezone().tzinfo\
    try:\
        return ZoneInfo(BOT_TZ)\
    except Exception:\
        return datetime.now().astimezone().tzinfo\
\
TZINFO = _get_tzinfo()\
\
def fmt_ts(ts_sec: int) -> str:\
    return datetime.fromtimestamp(ts_sec, tz=TZINFO).strftime("%H:%M:%S")\
\
def safe_int(x: Any, default: int = 0) -> int:\
    try:\
        return int(float(x))\
    except Exception:\
        return default\
\
def extract_ts_sec(c: Dict[str, Any]) -> Optional[int]:\
    for k in ("time", "t", "timestamp", "time_stamp"):\
        if k in c and c[k] is not None:\
            ts = safe_int(c[k], default=-1)\
            if ts <= 0:\
                continue\
            if ts > 10_000_000_000:\
                ts //= 1000\
            return ts\
    return None\
\
def extract_ohlc(c: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:\
    def gf(*keys: str) -> Optional[float]:\
        for k in keys:\
            if k in c and c[k] is not None:\
                try:\
                    return float(c[k])\
                except Exception:\
                    pass\
        return None\
\
    o = gf("open", "o")\
    h = gf("high", "h")\
    l = gf("low", "l")\
    cl = gf("close", "c")\
\
    if o is None or h is None or l is None or cl is None:\
        return None\
    return (o, h, l, cl)\
\
@dataclass\
class Candle:\
    ts: int\
    o: float\
    h: float\
    l: float\
    c: float\
\
def ensure_csv_header(path: str) -> None:\
    if os.path.exists(path) and os.path.getsize(path) > 0:\
        return\
    with open(path, "w", newline="", encoding="utf-8") as f:\
        w = csv.writer(f)\
        w.writerow(["time_local", "ts", "symbol", "O", "H", "L", "C"])\
\
def append_candle_csv(path: str, symbol: str, cd: Candle) -> None:\
    with open(path, "a", newline="", encoding="utf-8") as f:\
        w = csv.writer(f)\
        w.writerow([fmt_ts(cd.ts), cd.ts, symbol, cd.o, cd.h, cd.l, cd.c])\
\
def die(msg: str, code: int = 1) -> None:\
    print(f"[ERR] {msg}")\
    raise SystemExit(code)\
\
def make_client():\
    try:\
        from pocketoptionapi.stable_api import PocketOption  # type: ignore\
    except Exception as e:\
        die(f"Nenalezen pocketoptionapi.stable_api PocketOption. Chyba: {e}")\
\
    ssid = os.getenv("PO_SSID", "").strip()\
    if not ssid:\
        die("Chybí PO_SSID. Dej: export PO_SSID='...'\nPak znovu spusť script.")\
    return PocketOption(ssid)\
\
def connect(client) -> None:\
    ok = False\
    for fn_name in ("connect", "check_connect"):\
        fn = getattr(client, fn_name, None)\
        if callable(fn):\
            try:\
                res = fn()\
                ok = bool(res[0]) if isinstance(res, tuple) else bool(res)\
                if ok:\
                    break\
            except Exception:\
                pass\
    if not ok:\
        die("NepodaŃilo se pŃipojit k PocketOption (connect/check_connect selhalo).")\
\
def fetch_candles(client, symbol: str, timeframe_sec: int, count: int) -> List[Dict[str, Any]]:\
    now = int(time.time())\
    candidates = [\
        (symbol, timeframe_sec, count, now),\
        (symbol, timeframe_sec, now, count),\
        (symbol, timeframe_sec, count),\
        (symbol, timeframe_sec),\
    ]\
    last_exc = None\
    for args in candidates:\
        try:\
            res = client.get_candles(*args)  # type: ignore\
            if isinstance(res, list):\
                return res\
            if isinstance(res, dict):\
                for k in ("candles", "data", "result"):\
                    if k in res and isinstance(res[k], list):\
                        return res[k]\
        except Exception as e:\
            last_exc = e\
    if last_exc:\
        raise last_exc\
    return []\
\
RUNNING = True\
\
def _sig_handler(_sig, _frame):\
    global RUNNING\
    RUNNING = False\
\
def main():\
    global RUNNING\
    signal.signal(signal.SIGINT, _sig_handler)\
    signal.signal(signal.SIGTERM, _sig_handler)\
\
    ensure_csv_header(OUT_CSV)\
\
    client = make_client()\
    connect(client)\
\
    print(f"[OK] CONNECTED | symbol={SYMBOL} | tf={TIMEFRAME_SEC}s | tz={BOT_TZ} | csv={OUT_CSV}")\
\
    last_saved_ts = 0\
\
    try:\
        raw = fetch_candles(client, SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N)\
        parsed: List[Candle] = []\
        for c in raw:\
            if not isinstance(c, dict):\
                continue\
            ts = extract_ts_sec(c)\
            ohlc = extract_ohlc(c)\
            if ts is None or ohlc is None:\
                continue\
            o, h, l, cl = ohlc\
            parsed.append(Candle(ts, o, h, l, cl))\
        parsed.sort(key=lambda x: x.ts)\
        if parsed:\
            last_saved_ts = parsed[-1].ts\
            for cd in parsed[-3:]:\
                append_candle_csv(OUT_CSV, SYMBOL, cd)\
            print(f"[OK] WARMUP last_ts={last_saved_ts} ({fmt_ts(last_saved_ts)})")\
        else:\
            print("[WARN] WARMUP: žÃdnÃ data (prÃzdný seznam svíăek).")\
    except Exception as e:\
        print(f"[WARN] Warmup selhal: {e}")\
\
    while RUNNING:\
        try:\
            raw = fetch_candles(client, SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N)\
            candles: List[Candle] = []\
            for c in raw:\
                if not isinstance(c, dict):\
                    continue\
                ts = extract_ts_sec(c)\
                ohlc = extract_ohlc(c)\
                if ts is None or ohlc is None:\
                    continue\
                o, h, l, cl = ohlc\
                candles.append(Candle(ts, o, h, l, cl))\
\
            candles.sort(key=lambda x: x.ts)\
\
            new_ones = [cd for cd in candles if cd.ts > last_saved_ts]\
            for cd in new_ones:\
                append_candle_csv(OUT_CSV, SYMBOL, cd)\
                print(f"{fmt_ts(cd.ts)}  {SYMBOL}  O={cd.o:.5f} H={cd.h:.5f} L={cd.l:.5f} C={cd.c:.5f}")\
                last_saved_ts = cd.ts\
\
        except Exception as e:\
            print(f"[WARN] fetch/parse chyba: {e}")\
\
        time.sleep(POLL_SEC)\
\
    print("[OK] STOP")\
\
if __name__ == "__main__":\
    main()\
PY\
\
chmod +x minute_candles.py\
ls -la minute_candles.py\
\
source .venv/bin/activate\
\
export PO_SSID="$(cat po_ssid.txt)"\
export SYMBOL="CADCHF_otc"\
export BOT_TZ="local"\
\
python3 minute_candles.py\

cd /Users/macbook/bot-srdce\
source .venv/bin/activate\
\
python3 -m pip install -U pip setuptools wheel\
python3 -m pip install "git+https://github.com/ericpedra/pocketoptionapi.git"\
\
python3 -c "from pocketoptionapi.stable_api import PocketOption; print('OK pocketoptionapi import')"\

cd /Users/macbook/bot-srdce\
source .venv/bin/activate\
\
python3 -m pip install -U pip\
python3 -m pip install -U pocketoptionapi2==0.1.1\
\
python3 -c "from pocketoptionapi.stable_api import PocketOption; print('OK pocketoptionapi import')"\

export PO_SSID="$(tr -d '\n' < po_ssid.txt)"\
export SYMBOL="CADCHF_otc"\
export BOT_TZ="local"\
\
python3 minute_candles.py\

cd /Users/macbook/bot-srdce\
\
cat > minute_candles.py <<'PY'\
#!/usr/bin/env python3\
# -*- coding: utf-8 -*-\
\
import os\
import time\
import csv\
import signal\
from dataclasses import dataclass\
from datetime import datetime\
from typing import Any, Dict, List, Optional, Tuple\
\
try:\
    from zoneinfo import ZoneInfo\
except Exception:\
    ZoneInfo = None  # type: ignore\
\
SYMBOL = os.getenv("SYMBOL", "CADCHF_otc")\
TIMEFRAME_SEC = int(os.getenv("TIMEFRAME_SEC", "60"))\
FETCH_LAST_N = int(os.getenv("FETCH_LAST_N", "120"))\
POLL_SEC = float(os.getenv("POLL_SEC", "1.0"))\
OUT_CSV = os.getenv("OUT_CSV", f"candles_{SYMBOL}.csv")\
BOT_TZ = os.getenv("BOT_TZ", "local")\
\
# PO_DEMO: "1" => demo, "0" => real (default 0)\
PO_DEMO = os.getenv("PO_DEMO", "0").strip().lower() in ("1", "true", "yes", "y", "on")\
\
def _get_tzinfo():\
    if BOT_TZ.lower() == "local":\
        return datetime.now().astimezone().tzinfo\
    if ZoneInfo is None:\
        return datetime.now().astimezone().tzinfo\
    try:\
        return ZoneInfo(BOT_TZ)\
    except Exception:\
        return datetime.now().astimezone().tzinfo\
\
TZINFO = _get_tzinfo()\
\
def fmt_ts(ts_sec: int) -> str:\
    return datetime.fromtimestamp(ts_sec, tz=TZINFO).strftime("%H:%M:%S")\
\
def safe_int(x: Any, default: int = 0) -> int:\
    try:\
        return int(float(x))\
    except Exception:\
        return default\
\
def extract_ts_sec(c: Dict[str, Any]) -> Optional[int]:\
    for k in ("time", "t", "timestamp", "time_stamp"):\
        if k in c and c[k] is not None:\
            ts = safe_int(c[k], default=-1)\
            if ts <= 0:\
                continue\
            if ts > 10_000_000_000:\
                ts //= 1000\
            return ts\
    return None\
\
def extract_ohlc(c: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:\
    def gf(*keys: str) -> Optional[float]:\
        for k in keys:\
            if k in c and c[k] is not None:\
                try:\
                    return float(c[k])\
                except Exception:\
                    pass\
        return None\
\
    o = gf("open", "o")\
    h = gf("high", "h")\
    l = gf("low", "l")\
    cl = gf("close", "c")\
\
    if o is None or h is None or l is None or cl is None:\
        return None\
    return (o, h, l, cl)\
\
@dataclass\
class Candle:\
    ts: int\
    o: float\
    h: float\
    l: float\
    c: float\
\
def ensure_csv_header(path: str) -> None:\
    if os.path.exists(path) and os.path.getsize(path) > 0:\
        return\
    with open(path, "w", newline="", encoding="utf-8") as f:\
        w = csv.writer(f)\
        w.writerow(["time_local", "ts", "symbol", "O", "H", "L", "C"])\
\
def append_candle_csv(path: str, symbol: str, cd: Candle) -> None:\
    with open(path, "a", newline="", encoding="utf-8") as f:\
        w = csv.writer(f)\
        w.writerow([fmt_ts(cd.ts), cd.ts, symbol, cd.o, cd.h, cd.l, cd.c])\
\
def die(msg: str, code: int = 1) -> None:\
    print(f"[ERR] {msg}")\
    raise SystemExit(code)\
\
def make_client():\
    try:\
        from pocketoptionapi.stable_api import PocketOption  # type: ignore\
    except Exception as e:\
        die(f"Nenalezen pocketoptionapi.stable_api PocketOption. Chyba: {e}")\
\
    ssid = os.getenv("PO_SSID", "").strip()\
    if not ssid:\
        die("Chybí PO_SSID. Dej: export PO_SSID='...'\nPak znovu spusť script.")\
\
    # Různé verze mají různé signatury:\
    # - PocketOption(ssid, demo)\
    # - PocketOption(ssid=..., demo=...)\
    # - năkdy demo jako int/bool\
    demo = bool(PO_DEMO)\
\
    try:\
        return PocketOption(ssid, demo)  # nejăastăjŃí: (ssid, demo)\
    except TypeError:\
        pass\
    try:\
        return PocketOption(ssid=ssid, demo=demo)  # keyword varianta\
    except TypeError as e:\
        die(f"PocketOption init selhal i s demo parametrem. Chyba: {e}")\
\
def connect(client) -> None:\
    ok = False\
    for fn_name in ("connect", "check_connect"):\
        fn = getattr(client, fn_name, None)\
        if callable(fn):\
            try:\
                res = fn()\
                ok = bool(res[0]) if isinstance(res, tuple) else bool(res)\
                if ok:\
                    break\
            except Exception:\
                pass\
    if not ok:\
        die("NepodaŃilo se pŃipojit k PocketOption (connect/check_connect selhalo).")\
\
def fetch_candles(client, symbol: str, timeframe_sec: int, count: int) -> List[Dict[str, Any]]:\
    now = int(time.time())\
    candidates = [\
        (symbol, timeframe_sec, count, now),\
        (symbol, timeframe_sec, now, count),\
        (symbol, timeframe_sec, count),\
        (symbol, timeframe_sec),\
    ]\
    last_exc = None\
    for args in candidates:\
        try:\
            res = client.get_candles(*args)  # type: ignore\
            if isinstance(res, list):\
                return res\
            if isinstance(res, dict):\
                for k in ("candles", "data", "result"):\
                    if k in res and isinstance(res[k], list):\
                        return res[k]\
        except Exception as e:\
            last_exc = e\
    if last_exc:\
        raise last_exc\
    return []\
\
RUNNING = True\
\
def _sig_handler(_sig, _frame):\
    global RUNNING\
    RUNNING = False\
\
def main():\
    global RUNNING\
    signal.signal(signal.SIGINT, _sig_handler)\
    signal.signal(signal.SIGTERM, _sig_handler)\
\
    ensure_csv_header(OUT_CSV)\
\
    client = make_client()\
    connect(client)\
\
    print(f"[OK] CONNECTED | symbol={SYMBOL} | tf={TIMEFRAME_SEC}s | tz={BOT_TZ} | demo={int(PO_DEMO)} | csv={OUT_CSV}")\
\
    last_saved_ts = 0\
\
    try:\
        raw = fetch_candles(client, SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N)\
        parsed: List[Candle] = []\
        for c in raw:\
            if not isinstance(c, dict):\
                continue\
            ts = extract_ts_sec(c)\
            ohlc = extract_ohlc(c)\
            if ts is None or ohlc is None:\
                continue\
            o, h, l, cl = ohlc\
            parsed.append(Candle(ts, o, h, l, cl))\
        parsed.sort(key=lambda x: x.ts)\
        if parsed:\
            last_saved_ts = parsed[-1].ts\
            for cd in parsed[-3:]:\
                append_candle_csv(OUT_CSV, SYMBOL, cd)\
            print(f"[OK] WARMUP last_ts={last_saved_ts} ({fmt_ts(last_saved_ts)})")\
        else:\
            print("[WARN] WARMUP: žÃdnÃ data (prÃzdný seznam svíăek).")\
    except Exception as e:\
        print(f"[WARN] Warmup selhal: {e}")\
\
    while RUNNING:\
        try:\
            raw = fetch_candles(client, SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N)\
            candles: List[Candle] = []\
            for c in raw:\
                if not isinstance(c, dict):\
                    continue\
                ts = extract_ts_sec(c)\
                ohlc = extract_ohlc(c)\
                if ts is None or ohlc is None:\
                    continue\
                o, h, l, cl = ohlc\
                candles.append(Candle(ts, o, h, l, cl))\
\
            candles.sort(key=lambda x: x.ts)\
\
            new_ones = [cd for cd in candles if cd.ts > last_saved_ts]\
            for cd in new_ones:\
                append_candle_csv(OUT_CSV, SYMBOL, cd)\
                print(f"{fmt_ts(cd.ts)}  {SYMBOL}  O={cd.o:.5f} H={cd.h:.5f} L={cd.l:.5f} C={cd.c:.5f}")\
                last_saved_ts = cd.ts\
\
        except Exception as e:\
            print(f"[WARN] fetch/parse chyba: {e}")\
\
        time.sleep(POLL_SEC)\
\
    print("[OK] STOP")\
\
if __name__ == "__main__":\
    main()\
PY\
\
chmod +x minute_candles.py\

export PO_DEMO="1"\
python3 minute_candles.py\

cd /Users/macbook/bot-srdce\
ls -la candles_CADCHF_otc.csv\
tail -n 10 candles_CADCHF_otc.csv\

cd /Users/macbook/bot-srdce\
\
cat > minute_candles.py <<'PY'\
#!/usr/bin/env python3\
# -*- coding: utf-8 -*-\
\
import os\
import time\
import csv\
import signal\
from dataclasses import dataclass\
from datetime import datetime\
from typing import Any, Dict, List, Optional, Tuple\
\
from pocketoptionapi.stable_api import PocketOption\
\
SYMBOL = os.getenv("SYMBOL", "EURUSD")\
TIMEFRAME_SEC = 60\
FETCH_LAST_N = 120\
POLL_SEC = 1.0\
OUT_CSV = f"candles_{SYMBOL}.csv"\
PO_DEMO = os.getenv("PO_DEMO", "1") == "1"\
\
def fmt_ts(ts):\
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")\
\
@dataclass\
class Candle:\
    ts: int\
    o: float\
    h: float\
    l: float\
    c: float\
\
def ensure_csv():\
    if os.path.exists(OUT_CSV):\
        return\
    with open(OUT_CSV, "w", newline="") as f:\
        csv.writer(f).writerow(["time_local","ts","symbol","O","H","L","C"])\
\
def append(cd):\
    with open(OUT_CSV, "a", newline="") as f:\
        csv.writer(f).writerow([fmt_ts(cd.ts),cd.ts,SYMBOL,cd.o,cd.h,cd.l,cd.c])\
\
def main():\
    ssid = os.getenv("PO_SSID")\
    if not ssid:\
        print("PO_SSID chybí")\
        return\
\
    client = PocketOption(ssid, PO_DEMO)\
\
    print("PŃipojuji...")\
    client.connect()\
\
    print("Aktivuji symbol:", SYMBOL)\
    client.change_active(SYMBOL)\
\
    time.sleep(3)\
\
    ensure_csv()\
    last_ts = 0\
\
    print("START LOOP")\
\
    while True:\
        try:\
            candles = client.get_candles(SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N, int(time.time()))\
            if candles:\
                for c in candles:\
                    ts = int(c["time"])\
                    if ts > last_ts:\
                        cd = Candle(ts, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]))\
                        append(cd)\
                        print(fmt_ts(ts), cd.o, cd.h, cd.l, cd.c)\
                        last_ts = ts\
        except Exception as e:\
            print("ERROR:", e)\
\
        time.sleep(POLL_SEC)\
\
if __name__ == "__main__":\
    main()\
PY\

export PO_DEMO="1"\
export SYMBOL="EURUSD"\
python3 minute_candles.py\

cd /Users/macbook/bot-srdce\
source .venv/bin/activate\
\
python3 -c "from pocketoptionapi.stable_api import PocketOption; print('OK import venv')"\
\
export PO_SSID="$(tr -d '\n' < po_ssid.txt)"\
export PO_DEMO="1"\
export SYMBOL="EURUSD"\
\
python3 minute_candles.py\

cd /Users/macbook/bot-srdce\
source .venv/bin/activate\
\
cat > minute_candles.py <<'PY'\
#!/usr/bin/env python3\
# -*- coding: utf-8 -*-\
\
import os\
import time\
import csv\
import signal\
from dataclasses import dataclass\
from datetime import datetime\
from typing import Any, Dict, List, Optional, Tuple\
\
from pocketoptionapi.stable_api import PocketOption\
\
SYMBOL = os.getenv("SYMBOL", "EURUSD").strip()\
TIMEFRAME_SEC = int(os.getenv("TIMEFRAME_SEC", "60"))\
FETCH_LAST_N = int(os.getenv("FETCH_LAST_N", "120"))\
POLL_SEC = float(os.getenv("POLL_SEC", "1.0"))\
OUT_CSV = os.getenv("OUT_CSV", f"candles_{SYMBOL}.csv")\
\
PO_DEMO = os.getenv("PO_DEMO", "1").strip().lower() in ("1", "true", "yes", "y", "on")\
\
@dataclass\
class Candle:\
    ts: int\
    o: float\
    h: float\
    l: float\
    c: float\
\
def fmt_ts(ts: int) -> str:\
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")\
\
def ensure_csv_header(path: str) -> None:\
    if os.path.exists(path) and os.path.getsize(path) > 0:\
        return\
    with open(path, "w", newline="", encoding="utf-8") as f:\
        csv.writer(f).writerow(["time_local", "ts", "symbol", "O", "H", "L", "C"])\
\
def append_candle_csv(path: str, symbol: str, cd: Candle) -> None:\
    with open(path, "a", newline="", encoding="utf-8") as f:\
        csv.writer(f).writerow([fmt_ts(cd.ts), cd.ts, symbol, cd.o, cd.h, cd.l, cd.c])\
\
def extract_ts_sec(c: Dict[str, Any]) -> Optional[int]:\
    for k in ("time", "t", "timestamp", "time_stamp"):\
        if k in c and c[k] is not None:\
            ts = int(float(c[k]))\
            if ts > 10_000_000_000:\
                ts //= 1000\
            return ts\
    return None\
\
def extract_ohlc(c: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:\
    def gf(*keys: str) -> Optional[float]:\
        for k in keys:\
            if k in c and c[k] is not None:\
                try:\
                    return float(c[k])\
                except Exception:\
                    pass\
        return None\
    o = gf("open", "o")\
    h = gf("high", "h")\
    l = gf("low", "l")\
    cl = gf("close", "c")\
    if o is None or h is None or l is None or cl is None:\
        return None\
    return o, h, l, cl\
\
def try_activate_symbol(client, symbol: str) -> None:\
    """\
    Různé verze knihovny mají různé nÃzvy metod.\
    Tohle zkusí pÃr nejăastăjŃích a když nic, jen vypíŃe WARN.\
    """\
    candidates = [\
        ("change_active", (symbol,)),\
        ("set_active", (symbol,)),\
        ("set_symbol", (symbol,)),\
        ("set_asset", (symbol,)),\
        ("change_symbol", (symbol,)),\
        ("subscribe_symbol", (symbol,)),\
        ("subscribe", (symbol,)),\
        ("subscribe_candles", (symbol,)),\
        ("subscribe_candlestick", (symbol,)),\
    ]\
\
    for name, args in candidates:\
        fn = getattr(client, name, None)\
        if callable(fn):\
            try:\
                fn(*args)\
                print(f"[OK] Activated via {name}({symbol})")\
                return\
            except Exception as e:\
                print(f"[WARN] {name} existuje, ale selhalo: {e}")\
\
    print("[WARN] Nenalezena žÃdnÃ aktivaăní metoda (active/subscribe). Pokraăuju jen s get_candles().")\
\
RUNNING = True\
\
def _sig_handler(_sig, _frame):\
    global RUNNING\
    RUNNING = False\
\
def main():\
    global RUNNING\
    signal.signal(signal.SIGINT, _sig_handler)\
    signal.signal(signal.SIGTERM, _sig_handler)\
\
    ssid = os.getenv("PO_SSID", "").strip()\
    if not ssid:\
        print("[ERR] Chybí PO_SSID")\
        raise SystemExit(1)\
\
    ensure_csv_header(OUT_CSV)\
\
    client = PocketOption(ssid, PO_DEMO)\
\
    print("[INFO] PŃipojuji...")\
    # năkteré verze connect vrací bool/tuple, năkteré jen side-effect\
    try:\
        client.connect()\
    except Exception:\
        pass\
\
    try_activate_symbol(client, SYMBOL)\
\
    # dej knihovnă chvíli na WS handshake / pŃípadný subscribe\
    time.sleep(3)\
\
    print(f"[OK] START | symbol={SYMBOL} | tf={TIMEFRAME_SEC}s | demo={int(PO_DEMO)} | csv={OUT_CSV}")\
\
    last_saved_ts = 0\
\
    while RUNNING:\
        try:\
            now = int(time.time())\
            # různé podpisy: zkusíme více variant\
            raw = None\
            for args in (\
                (SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N, now),\
                (SYMBOL, TIMEFRAME_SEC, now, FETCH_LAST_N),\
                (SYMBOL, TIMEFRAME_SEC, FETCH_LAST_N),\
            ):\
                try:\
                    raw = client.get_candles(*args)\
                    if raw is not None:\
                        break\
                except TypeError:\
                    continue\
\
            candles: List[Candle] = []\
            if isinstance(raw, list):\
                for c in raw:\
                    if not isinstance(c, dict):\
                        continue\
                    ts = extract_ts_sec(c)\
                    ohlc = extract_ohlc(c)\
                    if ts is None or ohlc is None:\
                        continue\
                    o, h, l, cl = ohlc\
                    candles.append(Candle(ts, o, h, l, cl))\
\
            candles.sort(key=lambda x: x.ts)\
\
            new_ones = [cd for cd in candles if cd.ts > last_saved_ts]\
            for cd in new_ones:\
                append_candle_csv(OUT_CSV, SYMBOL, cd)\
                print(f"{fmt_ts(cd.ts)}  {SYMBOL}  O={cd.o:.5f} H={cd.h:.5f} L={cd.l:.5f} C={cd.c:.5f}")\
                last_saved_ts = cd.ts\
\
            if not new_ones:\
                # jemný heartbeat, ať víŃ že loop băží\
                print(f"[..] no new candles (last_ts={last_saved_ts})")\
\
        except Exception as e:\
            print(f"[WARN] loop chyba: {e}")\
\
        time.sleep(POLL_SEC)\
\
    print("[OK] STOP")\
\
if __name__ == "__main__":\
    main()\
PY\
\
export PO_SSID="$(tr -d '\n' < po_ssid.txt)"\
export PO_DEMO="1"\
export SYMBOL="EURUSD"\
\
python3 minute_candles.py\

# /Users/macbook/bot-srdce/minute_candles.py\
"""\
Money Printer   M1 candles (PocketOption)   polling pŃes get_candles()\
\
Použití (macOS, zsh):\
  cd ~/bot-srdce\
  source .venv/bin/activate\
  export PO_SSID="$(cat po_ssid.txt)"\
  export PO_IS_DEMO="1"\
  python minute_candles.py\
\
Volitelné env:\
  export PO_SYMBOL="EURUSD_otc"   # doporuăeno OTC\
  export PO_TF="60"              # 60 = M1\
  export PO_COUNT="150"\
  export PO_POLL="1.0"\
  export PO_CSV="candles_EURUSD_otc.csv"\
"""\
\
from __future__ import annotations\
\
import csv\
import inspect\
import os\
import sys\
import time\
from dataclasses import dataclass\
from datetime import datetime\
from typing import Any, Dict, Iterable, List, Optional, Tuple\
\
from pocketoptionapi_async.client import AsyncPocketOptionClient\
\
\
def _env_str(name: str, default: str) -> str:\
    v = os.getenv(name, "").strip()\
    return v if v else default\
\
\
def _env_int(name: str, default: int) -> int:\
    v = os.getenv(name, "").strip()\
    if not v:\
        return default\
    try:\
        return int(v)\
    except Exception:\
        return default\
\
\
def _env_float(name: str, default: float) -> float:\
    v = os.getenv(name, "").strip()\
    if not v:\
        return default\
    try:\
        return float(v)\
    except Exception:\
        return default\
\
\
def _read_ssid() -> str:\
    ssid = os.getenv("PO_SSID", "").strip()\
    if ssid:\
        return ssid\
    p = os.path.join(os.getcwd(), "po_ssid.txt")\
    if os.path.exists(p):\
        return open(p, "r", encoding="utf-8").read().strip()\
    return ""\
\
\
def _to_float(x: Any) -> Optional[float]:\
    try:\
        return float(x)\
    except Exception:\
        return None\
\
\
def _to_ts(x: Any) -> Optional[int]:\
    if x is None:\
        return None\
    if isinstance(x, datetime):\
        return int(x.timestamp())\
    try:\
        v = int(float(x))\
    except Exception:\
        return None\
    if v > 10**12:  # ms -> s\
        v //= 1000\
    return v\
\
\
@dataclass(frozen=True)\
class Candle:\
    ts: int\
    o: float\
    h: float\
    l: float\
    c: float\
\
    @property\
    def upper_wick(self) -> bool:\
        return self.h > max(self.o, self.c)\
\
    @property\
    def lower_wick(self) -> bool:\
        return self.l < min(self.o, self.c)\
\
    @property\
    def is_cross(self) -> bool:\
        # tvoje definice: kŃížek = mÃ 2 knoty (nahoru i dolů)\
        return self.upper_wick and self.lower_wick\
\
\
def _normalize_candle(item: Any) -> Optional[Candle]:\
    # Obj\
    if hasattr(item, "open") and hasattr(item, "high") and hasattr(item, "low") and hasattr(item, "close"):\
        ts = _to_ts(getattr(item, "timestamp", None) or getattr(item, "time", None) or getattr(item, "ts", None))\
        if ts is None:\
            return None\
        o = _to_float(getattr(item, "open", None))\
        h = _to_float(getattr(item, "high", None))\
        l = _to_float(getattr(item, "low", None))\
        c = _to_float(getattr(item, "close", None))\
        if None in (o, h, l, c):\
            return None\
        return Candle(ts=ts, o=o, h=h, l=l, c=c)\
\
    # list/tuple: [ts, open, close, high, low] (băžné)\
    if isinstance(item, (list, tuple)) and len(item) >= 5:\
        ts = _to_ts(item[0])\
        o = _to_float(item[1])\
        c = _to_float(item[2])\
        h = _to_float(item[3])\
        l = _to_float(item[4])\
        if ts is None or None in (o, h, l, c):\
            return None\
        return Candle(ts=ts, o=o, h=h, l=l, c=c)\
\
    # dict\
    if isinstance(item, dict):\
        ts = _to_ts(item.get("time") or item.get("timestamp") or item.get("t") or item.get("from") or item.get("ts"))\
        if ts is None:\
            return None\
        o = _to_float(item.get("open") or item.get("o"))\
        h = _to_float(item.get("high") or item.get("h"))\
        l = _to_float(item.get("low") or item.get("l"))\
        c = _to_float(item.get("close") or item.get("c"))\
        if None in (o, h, l, c):\
            return None\
        return Candle(ts=ts, o=o, h=h, l=l, c=c)\
\
    return None\
\
\
def _extract_candles(res: Any) -> List[Candle]:\
    if not res:\
        return []\
\
    # năkdy vrací tuple/list wrapper\
    if isinstance(res, (tuple, list)) and res and isinstance(res[0], list):\
        res = res[0]\
\
    # năkdy dict wrapper\
    if isinstance(res, dict):\
        for k in ("candles", "data", "items", "result"):\
            if k in res and isinstance(res[k], list):\
                res = res[k]\
                break\
\
    if not isinstance(res, list):\
        return []\
\
    out: List[Candle] = []\
    for it in res:\
        c = _normalize_candle(it)\
        if c:\
            out.append(c)\
\
    out.sort(key=lambda x: x.ts)\
    return out\
\
\
def _fmt_local(ts: int) -> str:\
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")\
\
\
def _ensure_otc_symbol(symbol: str) -> str:\
    # Pokud chceŃ jen OTC, tak tady to  vynutíme  když uživatel dÃ EURUSD bez suffixu.\
    s = symbol.strip()\
    if not s:\
        return s\
    if "_otc" in s.lower():\
        return s\
    # EURUSD -> EURUSD_otc (jen když uživatel dal ăistý forex)\
    if s.isalnum() and len(s) in (6, 7, 8):\
        return f"{s}_otc"\
    return s\
\
\
def _append_csv(path: str, rows: Iterable[Dict[str, Any]]) -> None:\
    rows = list(rows)\
    if not rows:\
        return\
\
    exists = os.path.exists(path)\
    with open(path, "a", newline="", encoding="utf-8") as f:\
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))\
        if not exists:\
            w.writeheader()\
        w.writerows(rows)\
\
\
async def _maybe_await(x: Any) -> Any:\
    return await x if inspect.isawaitable(x) else x\
\
\
def _parse_balance(value: Any) -> Optional[float]:\
    if value is None:\
        return None\
    if isinstance(value, (int, float)):\
        return float(value)\
    if isinstance(value, dict):\
        for k in ("balance", "amount", "value", "available", "total"):\
            if k in value:\
                v = _to_float(value[k])\
                if v is not None:\
                    return v\
        return None\
    for attr in ("balance", "amount", "value", "available", "total"):\
        if hasattr(value, attr):\
            v = _parse_balance(getattr(value, attr))\
            if v is not None:\
                return v\
    for dump_name in ("model_dump", "dict", "to_dict", "as_dict"):\
        if hasattr(value, dump_name):\
            try:\
                v = _parse_balance(getattr(value, dump_name)())\
                if v is not None:\
                    return v\
            except Exception:\
                pass\
    try:\
        return _parse_balance(vars(value))\
    except Exception:\
        return None\
\
\
async def main() -> None:\
    ssid = _read_ssid()\
    if not ssid:\
        print("[ERR] Chybí PO_SSID (env) nebo po_ssid.txt ve složce projektu.")\
        sys.exit(1)\
\
    is_demo = _env_str("PO_IS_DEMO", "1") in ("1", "true", "True", "yes", "YES")\
    symbol = _ensure_otc_symbol(_env_str("PO_SYMBOL", "EURUSD_otc"))\
    tf = _env_int("PO_TF", 60)\
    count = _env_int("PO_COUNT", 150)\
    poll = _env_float("PO_POLL", 1.0)\
    csv_path = _env_str("PO_CSV", f"candles_{symbol}.csv")\
\
    print("[INFO] PŃipojuji...")\
\
    client = AsyncPocketOptionClient(ssid, is_demo=is_demo, enable_logging=False)\
\
    # Pokud existuje change_symbol, zkusíme to sprÃvnă (symbol + period), ale je to optional.\
    if hasattr(client, "change_symbol"):\
        try:\
            await _maybe_await(client.change_symbol(symbol, tf))\
            print(f"[OK] change_symbol({symbol}, {tf})")\
        except Exception as e:\
            print(f"[WARN] change_symbol existuje, ale selhalo: {e}")\
\
    await client.connect()\
    print(f"[OK] START | symbol={symbol} | tf={tf}s | demo={int(is_demo)} | csv={csv_path}")\
\
    # Balance (optional)\
    bal = None\
    if hasattr(client, "get_balance"):\
        try:\
            raw = await _maybe_await(client.get_balance())\
            bal = _parse_balance(raw)\
        except Exception:\
            bal = None\
    if bal is not None:\
        print(f"[BAL] {bal:.2f}")\
\
    last_seen_ts: Optional[int] = None\
\
    # get_candles() signature je v různých verzích jinÃ => zkusíme nejdŃív keywordy, pak fallback\
    async def fetch() -> List[Candle]:\
        try:\
            res = await client.get_candles(asset=symbol, timeframe=tf, count=count)\
            return _extract_candles(res)\
        except TypeError:\
            res = await client.get_candles(symbol, tf, count)\
            return _extract_candles(res)\
\
    while True:\
        try:\
            candles = await fetch()\
            if not candles:\
                print("[WARN] 0 svíăek z API")\
                await asyncio.sleep(poll)\
                continue\
\
            max_ts = candles[-1].ts\
            age_sec = int(time.time()) - max_ts\
            if age_sec < 0:\
                age_sec = 0\
\
            # vezmeme jen NOVÃ (ts > last_seen_ts)\
            new_ones = []\
            for c in candles:\
                if last_seen_ts is None or c.ts > last_seen_ts:\
                    new_ones.append(c)\
\
            if new_ones:\
                rows = []\
                for c in new_ones:\
                    w = f"{'U' if c.upper_wick else '-'} {'L' if c.lower_wick else '-'}"\
                    typ = "KŃÃŽEK" if c.is_cross else "SVÃăKA"\
                    print(\
                        f"{_fmt_local(c.ts)} | {symbol} | {typ} | "\
                        f"O={c.o} H={c.h} L={c.l} C={c.c} | {w}"\
                    )\
                    rows.append(\
                        {\
                            "ts": c.ts,\
                            "time_local": _fmt_local(c.ts),\
                            "symbol": symbol,\
                            "type": typ,\
                            "open": c.o,\
                            "high": c.h,\
                            "low": c.l,\
                            "close": c.c,\
                            "upper_wick": int(c.upper_wick),\
                            "lower_wick": int(c.lower_wick),\
                        }\
                    )\
\
                _append_csv(csv_path, rows)\
                last_seen_ts = new_ones[-1].ts\
\
            # Debug heartbeat (ať vidíŃ jestli PO  stojí )\
            print(f"[DBG] fetched={len(candles)} max_ts={_fmt_local(max_ts)} age_sec={age_sec}")\
        except KeyboardInterrupt:\
            print("\n[INFO] stop")\
            break\
        except Exception as e:\
            print(f"[ERR] {e}")\
\
        await asyncio.sleep(poll)\
\
    try:\
        await client.disconnect()\
    except Exception:\
        pass\
\
\
if __name__ == "__main__":\
    import asyncio\
\
    asyncio.run(main())\

pwd\
ls -la\
ls -la po_ssid.txt\

cd ~/bot-srdce\
source .venv/bin/activate\
export PO_SSID="$(< po_ssid.txt)"\
export PO_IS_DEMO="1"\
export PO_SYMBOL="EURUSD_otc"\
python minute_candles.py\

/Users/macbook/bot-srdce/Run\ Bot.command ; exit;
/Users/macbook/bot-srdce/Run\ Bot.command ; exit;
/Users/macbook/bot-srdce/Run\ Bot.command ; exit;
/Users/macbook/bot-srdce/Run\ Bot.command ; exit;
/Users/macbook/bot-srdce/Run\ Bot.command ; exit;
