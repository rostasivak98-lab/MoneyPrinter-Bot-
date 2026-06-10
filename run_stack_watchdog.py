import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = str(ROOT / ".venv" / "bin" / "python3")
LOG_DIR = ROOT / "watchdog_logs"
LOG_DIR.mkdir(exist_ok=True)

CHECK_EVERY_SEC = 8
STARTUP_GRACE_SEC = 180
BAD_FOR_RESTART_SEC = 35
RESTART_COOLDOWN_SEC = 15
STREAM_STUCK_SEC = 45

COMPONENTS = {
    "worker_audnzd": {
        "kind": "worker",
        "symbol": "AUDNZD_otc",
        "port": 8011,
        "url": "http://127.0.0.1:8011/status",
        "cmd": [PYTHON, "dual_stream_test.py", "--symbol", "AUDNZD_otc", "--port", "8011"],
        "log": str(LOG_DIR / "worker_audnzd.log"),
    },
    "worker_eurchf": {
        "kind": "worker",
        "symbol": "EURCHF_otc",
        "port": 8013,
        "url": "http://127.0.0.1:8013/status",
        "cmd": [PYTHON, "dual_stream_test.py", "--symbol", "EURCHF_otc", "--port", "8013"],
        "log": str(LOG_DIR / "worker_eurchf.log"),
    },
    "worker_chfjpy": {
        "kind": "worker",
        "symbol": "CHFJPY_otc",
        "port": 8014,
        "url": "http://127.0.0.1:8014/status",
        "cmd": [PYTHON, "dual_stream_test.py", "--symbol", "CHFJPY_otc", "--port", "8014"],
        "log": str(LOG_DIR / "worker_chfjpy.log"),
    },
    "coordinator": {
        "kind": "coord",
        "port": 8020,
        "url": "http://127.0.0.1:8020/api/coord/status",
        "cmd": [PYTHON, "stream_coordinator_test.py"],
        "log": str(LOG_DIR / "coordinator.log"),
    },
}

PROCS = {}
HEALTH = {
    name: {
        "started_at": 0.0,
        "bad_since": None,
        "last_restart_ts": 0.0,
        "last_error": "—",
        "last_stream_closed_len": None,
        "last_progress_ts": 0.0,
    }
    for name in COMPONENTS
}

STOP = False


def now() -> float:
    return time.time()


def fetch_json(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def log_line(name: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{name}] {msg}", flush=True)


def start_component(name: str) -> None:
    spec = COMPONENTS[name]
    f = open(spec["log"], "a", buffering=1, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        spec["cmd"],
        cwd=str(ROOT),
        stdout=f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    PROCS[name] = {"proc": proc, "log_handle": f}
    HEALTH[name]["started_at"] = now()
    HEALTH[name]["bad_since"] = None
    HEALTH[name]["last_error"] = "—"
    HEALTH[name]["last_stream_closed_len"] = None
    HEALTH[name]["last_progress_ts"] = now()
    log_line(name, f"started pid={proc.pid}")


def stop_component(name: str) -> None:
    entry = PROCS.get(name)
    if not entry:
        return

    proc = entry["proc"]
    try:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            waited = 0.0
            while proc.poll() is None and waited < 6.0:
                time.sleep(0.2)
                waited += 0.2
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass

    try:
        entry["log_handle"].close()
    except Exception:
        pass

    PROCS.pop(name, None)
    log_line(name, "stopped")


def restart_component(name: str, reason: str) -> None:
    t = now()
    if (t - HEALTH[name]["last_restart_ts"]) < RESTART_COOLDOWN_SEC:
        return
    HEALTH[name]["last_restart_ts"] = t
    HEALTH[name]["last_error"] = reason
    log_line(name, f"restarting reason={reason}")
    stop_component(name)
    time.sleep(1.0)
    start_component(name)


def worker_healthy(name: str, data: dict):
    spec = COMPONENTS[name]
    symbol = spec["symbol"]
    started_at = HEALTH[name]["started_at"]

    if not isinstance(data, dict):
        return False, "invalid_json"

    got_symbol = str(data.get("symbol") or "")
    if got_symbol != symbol:
        return False, f"symbol_mismatch:{got_symbol}"

    sg = data.get("symbol_guard") or {}
    ui_ok = bool(sg.get("ui_symbol_ok"))
    active_mode = str(data.get("active_mode") or "")
    has_stream_current = bool(data.get("has_stream_current"))
    stream_closed_len = int(data.get("stream_closed_len") or 0)

    prev_len = HEALTH[name].get("last_stream_closed_len")
    if prev_len is None or stream_closed_len > int(prev_len):
        HEALTH[name]["last_progress_ts"] = now()
    HEALTH[name]["last_stream_closed_len"] = stream_closed_len

    # v grace periodu chceme jen to, aby worker zil a drzel spravny symbol endpoint
    if (now() - started_at) < STARTUP_GRACE_SEC:
        return True, "startup_grace_ok"

    # symbol v UI musi po grace sedet, ale PocketOption UI obcas kratce laguje
    if ui_ok:
        HEALTH[name]["last_ui_symbol_ok_ts"] = now()
    else:
        last_ui_ok = float(HEALTH[name].get("last_ui_symbol_ok_ts") or started_at)
        ui_bad_for = now() - last_ui_ok
        if ui_bad_for < STREAM_STUCK_SEC:
            return True, f"waiting_ui_symbol:{int(ui_bad_for)}s"
        return False, f"ui_symbol_not_ok:{int(ui_bad_for)}s"

    # kdyz je worker uz ve STREAM a ma current, je zdravy
    if active_mode == "STREAM" and has_stream_current and stream_closed_len >= 2:
        return True, "ok"

    # pokud stream/closed data nepostupuji dlouho, je zasekly
    last_progress_ts = float(HEALTH[name].get("last_progress_ts") or 0.0)
    stuck_for = now() - last_progress_ts

    if active_mode != "STREAM":
        if stuck_for < STREAM_STUCK_SEC:
            return True, f"waiting_stream:{active_mode}"
        return False, f"not_stream_stuck:{active_mode}"

    if not has_stream_current:
        if stuck_for < STREAM_STUCK_SEC:
            return True, "waiting_stream_current"
        return False, "missing_stream_current"

    if stream_closed_len < 2:
        if stuck_for < STREAM_STUCK_SEC:
            return True, f"warming_stream_closed:{stream_closed_len}"
        return False, f"stream_closed_too_low:{stream_closed_len}"

    return True, "ok"


def coord_healthy(data: dict):
    if not isinstance(data, dict):
        return False, "invalid_json"
    if not data.get("ok"):
        return False, "coord_not_ok"
    items = data.get("items") or []
    if len(items) < 3:
        return False, f"too_few_items:{len(items)}"
    return True, "ok"


def check_one(name: str) -> None:
    spec = COMPONENTS[name]
    entry = PROCS.get(name)

    if not entry:
        restart_component(name, "missing_process_entry")
        return

    proc = entry["proc"]
    if proc.poll() is not None:
        restart_component(name, f"process_exited:{proc.returncode}")
        return

    data = fetch_json(spec["url"])

    if spec["kind"] == "worker":
        ok, reason = worker_healthy(name, data)
    else:
        ok, reason = coord_healthy(data)

    if ok:
        HEALTH[name]["bad_since"] = None
        HEALTH[name]["last_error"] = "—"
        return

    if HEALTH[name]["bad_since"] is None:
        HEALTH[name]["bad_since"] = now()
        HEALTH[name]["last_error"] = reason
        log_line(name, f"unhealthy_started reason={reason}")
        return

    if (now() - HEALTH[name]["bad_since"]) >= BAD_FOR_RESTART_SEC:
        restart_component(name, reason)


def handle_stop(signum, frame):
    global STOP
    STOP = True
    log_line("watchdog", f"received_signal={signum}")


def main():
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    for name in COMPONENTS:
        start_component(name)
        time.sleep(2.0)

    while not STOP:
        for name in COMPONENTS:
            try:
                check_one(name)
            except Exception as e:
                log_line(name, f"watchdog_exception={type(e).__name__}:{e}")
        time.sleep(CHECK_EVERY_SEC)

    for name in list(COMPONENTS.keys())[::-1]:
        stop_component(name)


if __name__ == "__main__":
    main()
