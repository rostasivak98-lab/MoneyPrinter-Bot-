import csv
import time
import requests
from datetime import datetime, timezone

# Nastavení API a souboru pro ukládání svíček
API_BASE = "http://127.0.0.1:8001"
OUT_DIR = "."
STEP_SEC = 60
TICK_OFFSET_SEC = 20

def iso_utc(ts: int) -> str:
    """Převod timestampu na UTC časový formát"""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()

def get_status():
    """Získání statusu z API"""
    r = requests.get(f"{API_BASE}/api/status", timeout=5)
    r.raise_for_status()
    return r.json()

def wait_ready():
    """Čekání na připravenost API"""
    t0 = time.time()
    while True:
        try:
            d = get_status()
            c = d.get("candles") or []
            age = d.get("fetched_age_sec")
            if d.get("connected") and len(c) > 0 and age is not None and age <= 2:
                return
        except Exception:
            pass
        if time.time() - t0 > 90:
            return
        time.sleep(1)

def next_tick(step: int, offset: int) -> int:
    """Výpočet příštího ticku"""
    now = int(time.time())
    return ((now // step) + 1) * step + offset

def main():
    """Hlavní funkce pro spuštění bota"""
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "candles.csv")

    print(f"[bot] API_BASE={API_BASE} OUT={out_path} STEP_SEC={STEP_SEC} TICK_OFFSET_SEC={TICK_OFFSET_SEC}")
    wait_ready()

    header = [
        "tick_now_ts", "tick_now_utc", "ts_filled", "iso_utc_filled",
        "is_fill", "ts_raw", "iso_utc_raw", "ts_adj", "iso_utc_adj",
        "symbol", "status", "open", "high", "low", "close",
    ]
    wrote_header = os.path.exists(out_path) and os.path.getsize(out_path) > 0

    last_candle = None
    ts_filled = None  # Inicializováno z prvního raw

    tick_at = next_tick(STEP_SEC, TICK_OFFSET_SEC)
    print(f"[bot] next tick at {tick_at} ({iso_utc(tick_at)})")

    while True:
        # Aktualizace poslední svíčky
        try:
            d = get_status()
            candles = d.get("candles") or []
            if candles:
                c = candles[-1]
                ts = c.get("ts")
                if ts is not None:
                    last_candle = c
                    if ts_filled is None:
                        ts_filled = (int(ts) // STEP_SEC) * STEP_SEC
        except Exception:
            pass

        now = int(time.time())
        if now < tick_at:
            time.sleep(1)
            continue

        if last_candle is None or ts_filled is None:
            tick_at += STEP_SEC
            continue

        # Pokrok časového rámce
        ts_filled += STEP_SEC

        is_fill = 0
        if int(last_candle.get("ts")) < ts_filled:
            is_fill = 1  # No raw candle for this minute -> forward-fill last values

        ts_adj = int(last_candle.get("ts"))

        row = {
            "tick_now_ts": now,
            "tick_now_utc": iso_utc(now),
            "ts_filled": ts_filled,
            "iso_utc_filled": iso_utc(ts_filled),
            "is_fill": is_fill,
            "ts_raw": ts_adj,
            "iso_utc_raw": iso_utc(ts_adj),
            "ts_adj": ts_adj,
            "iso_utc_adj": iso_utc(ts_adj),
            "symbol": last_candle.get("symbol"),
            "status": last_candle.get("status"),
            "open": last_candle.get("open"),
            "high": last_candle.get("high"),
            "low": last_candle.get("low"),
            "close": last_candle.get("close"),
        }

        with open(out_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            if not wrote_header:
                w.writeheader()
                wrote_header = True
            w.writerow(row)

        print(f"[bot] wrote filled={ts_filled} is_fill={is_fill} ts_raw={ts_adj} close={row['close']}")
        tick_at += STEP_SEC

if __name__ == "__main__":
    main()

