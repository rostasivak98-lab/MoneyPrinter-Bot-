import os, time, json, csv
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from datetime import datetime, timezone

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8001").rstrip("/")
OUT = os.getenv("OUT", "./candles.csv")
POLL_SEC = float(os.getenv("POLL_SEC", "1.0"))
READY_TIMEOUT_SEC = int(os.getenv("READY_TIMEOUT_SEC", "90"))
START_MODE = os.getenv("START_MODE", "resume")  # "resume" | "from_now" | "fresh"
VERBOSE = os.getenv("VERBOSE", "1") == "1"

def http_get_json(url: str, timeout=5):
    req = Request(url, headers={"User-Agent": "csv_recorder/1.0"})
    with urlopen(req, timeout=timeout) as r:
        data = r.read()
    if not data:
        raise ValueError("empty response body")
    return json.loads(data.decode("utf-8"))

def load_last_written_ts(path: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", newline="") as f:
            rows = list(csv.reader(f))
        if len(rows) <= 1:
            return 0
        # auto-detect column
        header = rows[0]
        if "ts_raw" in header:
            idx = header.index("ts_raw")
        elif "ts" in header:
            idx = header.index("ts")
        else:
            return 0
        last = rows[-1][idx].strip()
        return int(last) if last else 0
    except Exception:
        return 0

def write_header_if_needed(path: str, header):
    need = (not os.path.exists(path)) or os.path.getsize(path) == 0
    if need:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)

def main():
    status_url = f"{API_BASE}/api/status"
    last_written_ts = 0

    if START_MODE == "fresh":
        # always overwrite
        open(OUT, "w").close()
        last_written_ts = 0
    elif START_MODE == "resume":
        last_written_ts = load_last_written_ts(OUT)
    elif START_MODE == "from_now":
        last_written_ts = 0

    print(f"[recorder] API_BASE={API_BASE} OUT={OUT} POLL_SEC={POLL_SEC} START_MODE={START_MODE}")
    if START_MODE == "resume":
        print(f"[recorder] resume from last_written_ts={last_written_ts}")

    # READY wait
    t0 = time.time()
    while True:
        try:
            d = http_get_json(status_url, timeout=5)
            c = d.get("candles") or []
            connected = d.get("connected")
            running = d.get("running")
            age = d.get("fetched_age_sec")
            if connected and running and len(c) > 0 and (age is None or age <= 2):
                print(f"[recorder] READY (candles_len={len(c)} age_sec={age})")
                if START_MODE == "from_now":
                    # start writing only after current newest ts
                    ts_list = sorted(int(x.get("ts")) for x in c if x.get("ts") is not None)
                    last_written_ts = ts_list[-1] if ts_list else 0
                    print(f"[recorder] from_now: set last_written_ts={last_written_ts}")
                break
            if VERBOSE:
                print(f"[recorder] waiting READY (connected={connected}, running={running}, candles={len(c)}, age={age}) timeout={READY_TIMEOUT_SEC}s")
        except Exception as e:
            if VERBOSE:
                print(f"[recorder] waiting READY (error={e}) timeout={READY_TIMEOUT_SEC}s")
        if time.time() - t0 > READY_TIMEOUT_SEC:
            raise SystemExit("[recorder] READY timeout")
        time.sleep(1)

    # Output format = write all unseen candles
    header = ["ts","iso_utc","symbol","status","open","high","low","close"]
    write_header_if_needed(OUT, header)

    last_progress_wall = time.time()

    while True:
        try:
            d = http_get_json(status_url, timeout=5)
            c = d.get("candles") or []
            if not c:
                if VERBOSE:
                    print(f"[recorder] no candles (connected={d.get('connected')} running={d.get('running')} err={d.get('last_candles_error','—')})")
                time.sleep(POLL_SEC)
                continue

            # normalize + sort
            rows = []
            for x in c:
                ts = x.get("ts")
                if ts is None:
                    continue
                try:
                    ts = int(ts)
                except Exception:
                    continue
                rows.append((ts, x))
            rows.sort(key=lambda t: t[0])

            wrote_any = False
            max_written = last_written_ts

            for ts, x in rows:
                if ts <= last_written_ts:
                    continue
                iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                line = [
                    ts,
                    iso,
                    x.get("symbol"),
                    x.get("status"),
                    x.get("open"),
                    x.get("high"),
                    x.get("low"),
                    x.get("close"),
                ]
                with open(OUT, "a", newline="") as f:
                    csv.writer(f).writerow(line)
                wrote_any = True
                max_written = ts
                print(f"[recorder] wrote ts={ts} status={x.get('status')} close={x.get('close')}")

            if wrote_any:
                last_written_ts = max_written
                last_progress_wall = time.time()
            else:
                # no new candles this poll
                stall = time.time() - last_progress_wall
                if stall > 90 and VERBOSE:
                    last_ts = rows[-1][0] if rows else 0
                    print(f"[recorder] STALL {int(stall)}s (no new ts). last_ts={last_ts} candles_len={len(rows)} connected={d.get('connected')} running={d.get('running')} age={d.get('fetched_age_sec')}")

        except (HTTPError, URLError, ValueError, json.JSONDecodeError) as e:
            if VERBOSE:
                print(f"[recorder] fetch error: {e}")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if VERBOSE:
                print(f"[recorder] unexpected error: {e}")

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
