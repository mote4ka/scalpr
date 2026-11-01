---
paths:
  - "packages/live/**"
  - "packages/core/src/scalpr_core/config.py"
---

# Реальная торговля (`scalpr-live`)

**Ордера на реальном счёте — только по явной команде пользователя, запускает их он сам.** Ключи не печатать
и не логировать (ни целиком, ни частично); `.env` читать только через `scalpr_core.config`.

## Что есть (01.11.2026)

- `scalpr_live/bybit.py`, `htx.py` — подписанный REST (aiohttp, query уходит ровно как подписан: `yarl.URL(encoded=True)`).
  Bybit v5: HMAC-SHA256 hex от `timestamp + key + recv_window + query|body`. HTX: base64 HMAC-SHA256 от
  `METHOD\nhost\npath\nsorted-query`, POST подписывает только auth-параметры, тело — JSON отдельно.
- `live check` — только чтение: ключи и права, баланс, режимы, комиссии аккаунта, размеры контрактов против конфига,
  минимальный хедж, маржа, открытые позиции/ордера, задержка REST. Тесты — `packages/live/tests`.
- `live run` — **dry run** слоя ордеров: живые ленты или `--replay` записи, биржи — `sim.SimExchange`, ничего не
  отправляется. Журнал `data/live/<start>/`: `orders|fills|hedges|snapshots|halts|specs.jsonl`, `run.json`.
  Реальных адаптеров бирж (выставление ордеров, приватные WS) ещё нет.

## Как устроен слой ордеров

- `trader.LiveTrader` держит paper-`Engine` рядом: его стаканы, базис и σ дают котировки через
  `Engine.decide(state, now, live_inventory)`, его paper-fills — двойник для сравнения (колонка `paper pnl`).
  Формулы котировок не копировать — только `decide`.
- `specs.Spec`: котировка → целые контракты HTX (`contracts`), хедж → шаги Bybit (`hedge_qty`), минимум Bybit
  (`min_hedge`: max(min_qty, $min_notional)). Контракт HTX обязан быть целым числом шагов Bybit (`steps_fit`).
- Размер котировки режется так, чтобы полный fill не вывел позицию HTX (включая ещё не захеджированное) за
  `max_position` — жёсткий лимит, в отличие от мягкого в paper.
- `orders.MakerOrders`: не больше одного ордера на сторону; пока биржа не ответила на place/cancel — сторона ждёт;
  новая цена = cancel, затем place после подтверждения (HTX не умеет amend). `Tolerance`: ордер не трогают, пока он
  не ближе к fair, чем `keep_inside`, не дальше `keep_outside` и не меньше `(1 − keep_size)` нужного размера.
  Ближе к fair / больше лимита / не нужен — срочная отмена.
- `trader.Budget`: token bucket запросов к HTX (`max_requests`/с, 30% — только для срочных отмен).
- `hedger.Hedger`: копит долг хеджа, отправляет целые ордера Bybit (IOC с лимитом `hedge_slippage`), недолив — снова
  в долг; остаток меньше минимума ждёт следующих fills.
- `account.Account`: две ноги раздельно (позиции, cash, комиссии `Config.fees`), `marked` по mid'ам.
- Остановки (котировки снимаются до конца прогона, хеджирование продолжается): ответа биржи нет `answer_timeout`,
  хеджируемый долг висит `hedge_timeout`, P&L ниже `-max_loss`. Пауза (без остановки): лента молчит `stale` (3 s).
- `[live]` в trader.toml: `stale`, `answer_timeout`, `hedge_timeout`, `max_loss`, `hedge_slippage`, `keep_inside`,
  `keep_outside`, `keep_size`, `max_requests` (`settings.py`, неизвестный ключ — ошибка).
- Сверка после изменений: `live run --replay data/stream-test/run1` с конфигом run1 (5 монет) и реплей обвала
  `data/stream-vps/crash`; при `keep_* = 0, max_requests = 0` слой следует каждому тику, как paper.

## Dry run: результаты (01.11.2026)

Реплей обвала (`data/stream-vps/crash`, 30 ч, DOT 0.25 / AVAX 0.2, 100/20 ms, skew 0.1, `hedge_fee = 0.1`):

| перестановка | запросов HTX | пик/с | fills | pnl | paper-двойник |
|---|---:|---:|---:|---:|---:|
| каждый тик, без бюджета (`keep_* = 0`) | 377 723 | 41 | 96 | +4.88 | +9.35 |
| inside 0.02 / outside 0.05 / size 0.2, 10/с | 102 057 | 17 | 86 | +5.76 | +9.35 |
| **inside 0.03 / outside 0.1 / size 0.3** (по умолчанию) | 67 659 | 16 | 109 | +7.47 | +9.35 |

- Следование каждому тику превышает лимит HTX (~24/с) в пиках и в среднем даёт ~3.5 запроса/с на монету.
- Слой берёт ~60–70% fills двойника: перестановка = cancel → подтверждение → place (2 × `quote_latency` без
  котировки на стороне), плюс жёсткий лимит позиции и пауза ленты уже через 3 s. Идея на потом: ставить новый ордер
  до подтверждения отмены (риск: два ордера на стороне) или batch-запросы.
- Пик бюджета до ~2× `max_requests` в календарную секунду (ведро на секунду + пополнение).
- Живые ленты с ПК, 5 мин: 158 запросов за ~4 мин котирования на 2 монеты (~0.65/с), пик 8/с, без ошибок и остановок.
- run1 (19 мин, 5 монет): каждый тик — 39 560 запросов (35/с, пик 104/с).

## Факты об API и аккаунтах (проверено запросами 01.11.2026)

- **HTX-аккаунт в multi-asset режиме (`asset_mode` 2): отвечает только V5.** v1/v3 методы аккаунта
  (`swap_cross_account_info`, `swap_unified_account_type`, ...) → ошибка 6023 «upgrade to V5». Публичные v1
  (`swap_contract_info`, `market/detail/merged`) и `POST /linear-swap-api/v1/swap_fee` работают.
- HTX V5 (хост `api.hbdm.com`, подпись та же, ответ `{"code": 200, "data"}`): GET `/v5/account/balance`,
  `/v5/account/asset_mode`, `/v5/position/mode` (`single_side`), `/v5/position/lever?contract_code&margin_mode=cross`,
  `/v5/trade/position/opens`, `/v5/trade/order/opens`, `/v5/trade/order`, `/v5/trade/order/details`.
  POST (по ccxt, ещё не вызывались): `/v5/trade/order` (`contract_code, volume` в контрактах, `side`, `type`
  `limit|post_only|market`, `price`, `margin_mode`, `position_side` `both`, `client_order_id`, `reduce_only`),
  `/v5/trade/batch_orders`, `/v5/trade/cancel_order`, `/v5/trade/cancel_batch_orders`, `/v5/trade/cancel_all_orders`,
  **`/v5/trade/cancel-after`** (автоотмена при молчании клиента), `/v5/position/lever`. Лимит ~24 торговых запроса/с.
  Приватный websocket V5 ещё не изучен. Официальная страница доков — SPA (WebFetch пустой); источники: ccxt `htx.py`,
  github.com/htx-exchange/htx-skills-hub.
- Состояние ключей, балансы и позиции аккаунтов — в личной памяти Claude, не в репозитории (он публичный);
  актуальное — `live check`. Bybit UTA, `REGULAR_MARGIN`, one-way.
- **Комиссии аккаунта Bybit: maker 0.036%, taker 0.10% на всех линейных парах** (модель `FEES`: taker 0.055%).
  HTX `swap_fee`: maker 0.02%, taker 0.06%. Хеджированный fill стоит 0.12% против 0.075% в модели.
- Контракты: HTX DOT и AVAX — 1 монета/контракт; Bybit шаг 0.1, минимум 0.1 и $5 — fill DOT меньше 6 контрактов
  отдельно не хеджируется (остатки копить). Плечо 10x на обеих.
- На аккаунте могут быть чужие для бота (ручные) позиции и ордера с общей cross-маржой: бот трогает только свои
  ордера (`client_order_id`/`orderLinkId` из своего диапазона).
- REST с ПК пользователя: HTX ~320 ms, Bybit ~250 ms (VPS не мерили).

## Влияние реальных комиссий (архивы 14–23.10, walk-forward, $ за 7 дней, 100/20 ms)

Пересчёт `pnl − объём × 0.045%` (комиссия не меняет fills): fair 21.7 → 13.9 (13/14 дней в плюсе),
skew 0.1: 41.1 → 22.5 (14/14), то есть ~$3.2/день на две монеты при котировке $100. Edge по walk-forward
сдвигается к 0.25–0.3; сетка до 0.5 проверена: 0.35+ хуже (за 10 дней skew 0.1, DOT/AVAX: 0.25 → 26.8/11.7,
0.3 → 24.9/11.1, 0.35 → 18.0/5.9, 0.5 → 7.6/−1.2).

## План слоя исполнения

1. Готово: `live check`.
2. Готово: комиссии аккаунта в конфиге (`maker_fee`/`hedge_fee`).
3. Готово: слой ордеров + dry run (`live run`, симулятор бирж, paper-двойник, бюджет запросов, остановки).
4. Дальше: реальные адаптеры с тем же интерфейсом, что `sim.SimExchange` (`place/cancel/hedge/tick/on_trade` +
   ответы `answered/canceled/filled/hedged`): HTX V5 post_only + приватный WS (fills, ответы; формат ещё изучить),
   Bybit IOC + приватный WS; `cancel-after` на HTX; при старте — отмена своих ордеров (id из своего диапазона) и сверка
   позиций обеих бирж с `Account`, периодическая сверка.
5. Замер задержки с VPS: post_only далеко от рынка (не исполнятся) + отмена — запускает пользователь.
6. Минимальный размер (котировка ~$10–20, лимит ~$30), 1–2 недели сравнения с двойником, потом рост.

Предусловия (на стороне пользователя): деньги на фьючерсных счетах; отдельные субаккаунты под бота (не делить маржу
с ручными позициями); ключи с привязкой к IP VPS; вход на VPS только по ключам; точные часы там, где запущен бот.
