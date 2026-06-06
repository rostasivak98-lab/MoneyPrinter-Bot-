# BOT-SRDCE HANDOFF

## Co je BOT-SRDCE

BOT-SRDCE je PocketOption trading bot postavený kolem:

- Python backendu
- FastAPI API
- Playwright websocket tick feedu
- Heikin Ashi market structure
- multi-symbol coordinatoru
- dashboardu
- Telegram integrace
- structure engine logiky

Cíl není klasický indicator bot.

Cíl je číst strukturu trhu jako člověk.

## Hlavní stavy

- 🟢 CLEAN BUY
- 🔴 CLEAN SELL
- 🟡 BUY
- 🟠 SELL
- 🟦 BUY PULLBACK
- 🟪 SELL PULLBACK
- ⚫ CHAOS

## Filozofie strategie

Bot NEjede:

- RSI crossover
- EMA cross
- random indikátory

Bot čte:

- dominanci směru
- délku bloků
- alternace
- chaos
- momentum
- pullbacky
- continuation
- strukturu posledních candle

Používá hlavně:

- Heikin Ashi
- market structure
- continuation logic
- trend continuation
- chaos filtering

## Důležité pravidlo

Nezačínat od nuly.

Nedělat velký refactor.

Dělat jen malé surgical patche podle reálného debug výstupu.

## Patch workflow

Vždy:

1. logy
2. grep
3. sed okolí funkce
4. malý surgical patch
5. py_compile
6. restart
7. debug
8. teprve potom Git commit/push

Nikdy:

- nehádat strukturu souboru
- nepřepisovat celý endpoint
- nepřepisovat celý engine
- nepřidávat náhodné ify
- nerestartovat před py_compile

## Spuštění coordinator stacku

```bash
cd ~/bot-srdce

pkill -f "run_stack_watchdog.py" || true
pkill -f "stream_coordinator_test.py" || true
pkill -f "dual_stream_test.py" || true

sleep 2

source .venv/bin/activate

python3 -m py_compile stream_coordinator_test.py

python3 run_stack_watchdog.py
