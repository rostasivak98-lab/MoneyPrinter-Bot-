from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from pocketoptionapi_async import AsyncPocketOptionClient


class BotEngine:
    def __init__(self, tick_interval_s: float = 1.0) -> None:
        self._tick_interval_s = float(tick_interval_s)
        self._running: bool = False
        self._ticks: int = 0
        self._last_tick: Optional[Dict[str, Any]] = None
        self._client: Optional[AsyncPocketOptionClient] = None

    def _get_config(self) -> tuple[str, bool]:
        ssid = os.getenv("PO_SSID", "").strip()
        is_demo = os.getenv("PO_IS_DEMO", "1").strip() not in {"0", "false", "False"}
        return ssid, is_demo

    async def start(self) -> tuple[bool, str]:
        if self._running:
            return False, "already running"

        ssid, is_demo = self._get_config()
        if not ssid:
            return False, "missing PO_SSID (expected 42[\"auth\",{...}])"

        self._client = AsyncPocketOptionClient(ssid, is_demo=is_demo, enable_logging=True)
        await self._client.connect()
        self._running = True
        return True, "started"

    async def stop(self) -> tuple[bool, str]:
        if not self._running:
            return False, "not running"

        self._running = False
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None
        return True, "stopped"

    async def tick_once(self) -> None:
        if not self._running or self._client is None:
            return

        bal = await self._client.get_balance()
        self._ticks += 1
        self._last_tick = {
            "timestamp": time.time(),
            "balance": getattr(bal, "balance", None),
            "currency": getattr(bal, "currency", None),
            "is_demo": getattr(bal, "is_demo", None),
        }

    def status(self) -> Dict[str, Any]:
        return {"running": self._running, "ticks": self._ticks, "last_tick": self._last_tick}


engine = BotEngine(tick_interval_s=1.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    import asyncio

    async def loop() -> None:
        while True:
            try:
                await engine.tick_once()
            except Exception as e:
                engine._last_tick = {"error": str(e), "timestamp": time.time()}
            await asyncio.sleep(1.0)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await engine.stop()
        except Exception:
            pass


app = FastAPI(title="Bot – srdce (PocketOption)", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(
        """
<!doctype html>
<html lang="cs">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Bot – srdce (PocketOption)</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; max-width: 900px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0 16px; }
    button { padding: 12px 18px; font-size: 16px; border-radius: 10px; border: 1px solid #ccc; cursor: pointer; }
    button:disabled { opacity: .6; cursor: not-allowed; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 14px; }
    pre { white-space: pre-wrap; word-break: break-word; margin: 0; }
  </style>
</head>
<body>
  <h1>Bot – srdce (PocketOption)</h1>

  <div class="row">
    <button id="startBtn">▶ Start</button>
    <button id="stopBtn">■ Stop</button>
    <button id="refreshBtn">⟳ Refresh</button>
  </div>

  <div class="card">
    <div><b>Stav:</b> <span id="running">?</span> | <b>Ticks:</b> <span id="ticks">?</span></div>
    <div style="margin-top:10px;"><b>Last tick:</b></div>
    <pre id="lastTick">{}</pre>
  </div>

<script>
  const $ = (id) => document.getElementById(id);

  async function call(method, url) {
    const res = await fetch(url, { method });
    return res.json();
  }

  function setButtons(running) {
    $("startBtn").disabled = running;
    $("stopBtn").disabled = !running;
  }

  async function refresh() {
    const st = await call("GET", "/status");
    $("running").textContent = st.running ? "běží" : "stojí";
    $("ticks").textContent = st.ticks ?? 0;
    $("lastTick").textContent = JSON.stringify(st.last_tick, null, 2);
    setButtons(!!st.running);
  }

  $("startBtn").addEventListener("click", async () => { await call("POST", "/start"); await refresh(); });
  $("stopBtn").addEventListener("click", async () => { await call("POST", "/stop"); await refresh(); });
  $("refreshBtn").addEventListener("click", refresh);

  refresh();
  setInterval(refresh, 1000);
</script>
</body>
</html>
        """.strip()
    )


@app.post("/start")
async def start_bot():
    ok, msg = await engine.start()
    return JSONResponse({"status": msg, "ok": ok})


@app.post("/stop")
async def stop_bot():
    ok, msg = await engine.stop()
    return JSONResponse({"status": msg, "ok": ok})


@app.get("/status")
def status():
    return JSONResponse(engine.status())
