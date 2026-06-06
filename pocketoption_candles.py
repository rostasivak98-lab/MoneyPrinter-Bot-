import time
import json
import websockets
import asyncio

# Parametry pro připojení k API
PO_SSID = "tvůj_ssid"  # Nahraď vlastním PO_SSID
API_URL = "wss://api.pocketoption.com/ws"  # Adresa websocketu pro PocketOption

# Funkce pro získání svíček
async def fetch_candles(symbol="BTCUSD", timeframe="1m"):
    async with websockets.connect(API_URL) as ws:
        # Pošleme požadavek na svíčky
        request = {
            "type": "get_candles",
            "symbol": symbol,
            "timeframe": timeframe,
            "ssid": PO_SSID
        }
        await ws.send(json.dumps(request))
        response = await ws.recv()
        data = json.loads(response)
        print("Candles Data:", data)
        return data

# Funkce pro práci s Heiken Ashi svíčkami
def calculate_heiken_ashi(candles):
    heiken_ashi = []
    for i in range(1, len(candles)):
        open_ = (candles[i-1]['open'] + candles[i-1]['close']) / 2
        close = (candles[i]['open'] + candles[i]['high'] + candles[i]['low'] + candles[i]['close']) / 4
        high = max(candles[i]['high'], open_, close)
        low = min(candles[i]['low'], open_, close)
        heiken_ashi.append({
            "open": open_,
            "close": close,
            "high": high,
            "low": low
        })
    return heiken_ashi

# Asynchronní funkce pro spuštění
async def main():
    candles = await fetch_candles(symbol="BTCUSD", timeframe="1m")
    heiken_ashi = calculate_heiken_ashi(candles)
    print("Heiken Ashi Data:", heiken_ashi)

# Spuštění
if __name__ == "__main__":
    asyncio.run(main())

