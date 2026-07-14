# PROJECT STATE

Poslední update: 2026-06-07

## HOTOVÉ

✅ Playwright websocket feed

✅ Multi symbol coordinator

✅ Candle engine

✅ Heikin Ashi engine

✅ Structure engine

✅ CLEAN BUY

✅ CLEAN SELL

✅ BUY

✅ SELL

✅ BUY PULLBACK

✅ SELL PULLBACK

✅ CHAOS

✅ Dashboard základ

✅ GitHub repo

✅ Git workflow

## ROZPRACOVANÉ

🔄 Live validace zaver vs graf

## DALŠÍ PRIORITY

1. trade_confidence

2. dashboard redesign

3. Telegram /status

4. Telegram /best

5. Telegram /chaos

6. entry_state

7. entry timing

8. auto trading

## POSLEDNÍ DŮLEŽITÉ ZMĚNY

2026-06-07

- GitHub propojen a funkční
- .venv odstraněno z Gitu
- watchdog_logs odstraněny z Gitu
- vytvořen BOT_SRDCE_HANDOFF.md
- vytvořen PROJECT_STATE.md


## CHECKPOINT 2026-07-14 — Git cleanup

- Cíl první verze: signály pro ruční vstup.
- Rozšířen `.gitignore` pro ochranu přihlašovacích údajů a lokálních souborů.
- Python `__pycache__` a `.pyc` soubory odstraněny z evidence Gitu.
- Zálohy, checkpointy, logy, databáze a ZIP archivy nebudou commitovány.
- Hlavní Python soubory prošly kontrolou syntaxe.

## CHECKPOINT 2026-07-14 — best_setup bias guard

- Oprava: best_setup se vybere jen když směr (zaver) souhlasí s market_bias.
- Přidán market_bias a market_bias_reason do výstupu structure_debug.
- Ověřeno živě: při market_bias=CHAOS je best_setup správně null.
- Příklad: EURCHF měl BUY PULLBACK 76 %, ale market_bias=CHAOS → správně odmítnut.
- py_compile OK, koordinátor restartován přes watchdog.
