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
- `live run` — слой ордеров: живые ленты или `--replay` записи; по умолчанию **dry run** (`sim.SimExchange`, ничего не
  отправляется), с `--real` — `real.RealExchange`. Журнал `data/live/<start>/`:
  `orders|fills|hedges|snapshots|halts|specs|errors|positions.jsonl`, `run.json` (`dry_run`).
- `live probe [--rounds N] [--real]` — задержка ордеров: post_only-покупка DOT на 3% ниже bid (HTX 1 контракт,
  Bybit минимум $5), сразу отмена; REST place/cancel и время до push в приватном WS. Без `--real` только план.
  В начале и в конце `real.prepare` (отменить всё, показать позиции).
- `live.example.toml` — первый реальный запуск на ~$10 на биржу (котировка $10, лимит $20/монету, `hedge_fee = 0.1`,
  `max_loss = 3`); копию держать в `data/` (не коммитится).
- **Реальных ордеров ещё не было** (на 01.11.2026): адаптеры проверены тестами на поддельной бирже, приватные WS —
  входом и подпиской на реальных субаккаунтах. Первый реальный шаг — `live probe --real` (запускает пользователь).

## Реальный режим (`real.py`, `private.py`)

- Приватные WS: HTX `wss://api.hbdm.com/ws/v5/notification` (auth `{"op":"auth","type":"api",AccessKeyId,…,Signature}`,
  подпись как GET пути `/ws/v5/notification`; gzip; сервер шлёт `{"op":"ping"}` ~раз в 10 s → `pong`; подписка
  `{"op":"sub","topic":"orders","contract_code":"*"}`). Bybit `wss://stream.bybit.com/v5/private` (auth
  `{"op":"auth","args":[key, expires_ms, hex HMAC("GET/realtime"+expires)]}`, ping каждые 20 s, топик `order`).
  Оба сообщения приводятся к `OrderUpdate` (накопительно: исполнено, оборот USDT, финальное ли состояние).
- HTX `orders`: состояния new / partially_filled / filled / partially_canceled / canceled; финал — когда
  `trade_volume + cancel_volume == volume` (несколько push «filled» на один матч). Fill = прирост `trade_volume`,
  цена = прирост `trade_turnover` / (контракты × размер контракта).
- `RealExchange`: maker — `POST /v5/trade/order` post_only one-way cross с нашим `client_order_id` (целое, из
  времени старта); отмена `/v5/trade/cancel_order` (1061 «нет ордера» — не ошибка, итог скажет WS; иначе 2 попытки);
  хедж — Bybit `POST /v5/order/create` Limit IOC с лимитом `hedge_slippage`, `orderLinkId` `x<ms>-<n>`, ответ — финальный
  push. Отказ биржи → ответ сразу (rejected / хедж с нулём); таймаут запроса → ждём WS, нет ответа → таймауты трейдера.
  `cancel-after` (5 s) обновляется каждые 1.5 s; 3 сбоя подряд → остановка. Fill по уже забытому ордеру всё равно
  учитывается и хеджируется (`Listener.filled` получает сторону).
- Старт (`go_real`): приватные WS готовы → `prepare` (отменить все ордера субаккаунтов, проверить, что их не осталось,
  прочитать позиции; позиция в монете не из конфига — отказ) → ноги, которые не гасят друг друга больше
  минимального хеджа, — отказ; иначе `LiveTrader.restore`. Выход (любой): `RealExchange.close` — снять все ордера HTX.
- Остановки реального режима сверх dry run: приватный WS упал (`trouble`, навсегда до рестарта), 5 отказов в ордерах
  за минуту, сбои `cancel-after`. При остановке котировки снимаются, хеджи продолжаются; ноги, не сошедшиеся из-за
  потерянных ответов, всплывут при следующем старте (`prepare` откажет).
- Лимиты HTX V5: торговые 72 запроса / 3 s на UID, чтение 72 / 3 s; `cancel-after` ≥ 5000 ms и снимает **все**
  ордера аккаунта — поэтому только субаккаунт бота. Источник: официальная SPA-документация HTX (читается
  через встроенный браузер, WebFetch пустой), раздел «USDT-Margined Futures Multi-Assets Collateral».

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
  POST (официальные доки, вызовов ещё не было): `/v5/trade/order` (`contract_code, margin_mode, side, type`
  `limit|post_only|market`, `price`, `volume` строкой в контрактах, `position_side` `both`, `client_order_id` целое
  1…2^63−1, `reduce_only`, `time_in_force` gtc|ioc|fok), `/v5/trade/cancel_order` (`contract_code` + `order_id` или
  `client_order_id`), `/v5/trade/cancel_all_orders` (`contract_code`, `side` — необязательны),
  **`/v5/trade/cancel-after`** (`on_off` "1"/"0", `time_out` ≥ 5000 ms), `/v5/trade/batch_orders`,
  `/v5/position/lever`. GET `/v5/trade/position/opens`: `contract_code, direction buy|sell, volume` (контракты), …
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
4. Готово: реальные адаптеры (`real.py`, `private.py`), `live probe`, пример `live.example.toml`.
5. Дальше (запускает пользователь): `live probe --real` с ПК, потом с VPS; затем `live run --real` на ~$10.
   Не сделано: периодическая сверка позиций во время работы (только при старте), опрос REST вместо потерянных
   push'ей, переподключение приватного WS без остановки, ключи с привязкой к IP VPS, деплой `scalpr-live` на VPS.
6. 1–2 недели на минимальном размере со сравнением с двойником, потом рост.

Предусловия (на стороне пользователя): деньги на фьючерсных счетах; отдельные субаккаунты под бота (не делить маржу
с ручными позициями); ключи с привязкой к IP VPS; вход на VPS только по ключам; точные часы там, где запущен бот.
