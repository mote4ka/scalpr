# scalpr

Исследование и paper-симуляция межбиржевого маркет-мейкинга (XEMM) на USDT-перпетуалах:
**HTX — maker** (пассивные котировки), **Bybit — hedge** (тейкер сразу после fill на HTX).
Fair price на HTX = mid Bybit, сдвинутый на rolling-медиану базиса HTX vs Bybit.
Это не арбитраж «купил дешевле/продал дороже»: котируем постоянно, хеджируемся по факту исполнения.

Текущая фаза — **paper trading**. Реальных ордеров, приватных API и ключей в коде нет
(`scalpr_core/config.py` только читает `BYBIT_API`/`HTX_API` из `.env`, нигде не используется).

## Рабочие правила

- Общение с пользователем — по-русски. Код, комментарии и сообщения коммитов — по-английски, как в истории.
- **Без явного разрешения: никаких `git push`, реальных ордеров, действий на VPS и с API-ключами.**
  Локальные коммиты — только когда пользователь их разрешил в текущем разговоре.
- Не менять `packages/trader/config.example.toml` без прямой просьбы.
- Не коммитить `data/`, `.env`, `trader.toml`, журналы и записи потоков.
- Пароли/реквизиты из старых переписок не использовать и не искать; доступ к серверу — только SSH-ключи.
- Новые запускаемые утилиты — пакетами в `packages/`, без папки `apps/`; запуск через `uv run --package`.
- Для данных — pandas/numpy и Parquet; для CLI — Rich (прогресс скачивания и обработки раздельно).
- Не делать выводов о прибыльности по gross spread: только net после комиссий, hedge VWAP, задержек, инвентаря, markout.
- Edge выбирается на train-окне (walk-forward / `--train-until`), не на тестовом периоде.

## Монорепозиторий (uv workspace, `packages/*`, Python 3.11)

| Пакет | Модуль | Назначение |
|---|---|---|
| `scalpr-core` | `scalpr_core` | пути (`PROJECT_ROOT` по `uv.lock` или `SCALPR_ROOT`, `DATA_DIR=data/`, `LOGS_DIR=data/logs`), Rich-логгер, `retry` |
| `scalpr-strategy` | `scalpr_strategy` | чистая логика без I/O: `FEES`, `fair_price`, `quote_prices`, `filled`/`FILL_RULES`, `markout`, `Ledger` |
| `scalpr-collector` | `scalpr_collector` | архивы бирж → Parquet (`exchanges/`), live websocket-фиды, локальные стаканы, запись и replay (`realtime/`) |
| `scalpr-trader` | `scalpr_trader` | paper-движок на live/записанных фидах: `engine`, `paper` (PaperQuotes), `basis`, `journal`, `dashboard` |
| `scalpr-scanner` | `scalpr_scanner` | спред/базис, скринер, векторный XEMM-бэктест (`xemm.py`), walk-forward, токсичность, `paper-report` |

Зависимости: strategy ← trader ← scanner (scanner импортирует `scalpr_trader.config/journal` и глубины фидов).
Формулы стратегии не дублировать: и бэктест (`scanner/xemm.py`), и трейдер (`trader/engine.py`) берут их из `scalpr_strategy`.

## Команды

```powershell
uv sync
uv run --package scalpr-collector stream --tickers DOTUSDT,AVAXUSDT [--minutes N] [--no-record]
uv run --package scalpr-scanner scanner <TICKER> <YYYY-MM-DD> [--strategy basis|xemm]   # один день + plotly-график
uv run --package scalpr-scanner screener [--latency 50ms|200ms]
uv run --package scalpr-scanner backtest --tickers A,B --days 7 --edges 0.1,0.15,0.2,0.25 --latencies 50ms,200ms,500ms --walk-forward 3
uv run --package scalpr-trader trader --config <trader.toml> [--minutes N] [--record [DIR]] [--replay DIR] [--no-journal]
uv run --package scalpr-scanner paper-report [data/paper/<start>] [--source auto|archive|<recording>]
uv run ruff check .
```

- Entry point `collector` — заглушка (печатает hello); исторические дни качаются через `scanner`/`backtest`/`paper-report`.
- `backtest --latencies` задаёт **одну** задержку и для перестановки котировки, и для хеджа; раздельные
  `quote_latency`/`hedge_latency` есть в `xemm(..., hedge_latency=)` и используются `paper-report`.
- Архивы публикуются с лагом: последний полный день — позавчера UTC (`history.PUBLISH_LAG`).
- `trader.toml`: в `[trader]` — `maker` (только `"htx"`), `notional`, `max_position`, `quote_latency`, `hedge_latency`,
  `fill`, `basis_window`, `warmup`, `stale`, `markout`; в `[coins.<TICKER>]` — `edge` и опционально свои
  `notional`/`max_position`. Тикер — имя таблицы. Локального `packages/trader/trader.toml` сейчас нет.

## Проверки

- **Тестов в репозитории нет** (pytest есть в dev-группе, файлов тестов нет). Прошлые сверки (replay ↔ REST,
  paper ↔ backtest 8/8 fills, регрессия XEMM 12/12) делались разовыми скриптами — при изменении модели их
  нужно воспроизводить заново или оформлять в настоящие тесты.
- Ruff без проектного конфига (дефолты ruff 0.17). Новый код чистый; 21 замечание — в старых
  `scalpr_core/{logger,retry,paths,config}.py` и `collector/http_client.py`. **`ruff format` не запускать**:
  стиль проекта — длинные строки, форматтер переписал бы 34 файла.
- Для realtime-изменений — короткий `trader --minutes N` или `--replay data/stream-test/run1`, затем `paper-report`.
- Не заявлять об успешной проверке, если она не запускалась.

## Данные (`data/`, в git не попадает)

`history/<exchange>/<inst>/` (архивы + Parquet), `stream/<exchange>/<YYYY-MM-DD>/<HH>.jsonl.gz` (строка `{"r": recv ns, "m": msg}`),
`paper/<YYYY-MM-DD_HHMMSS>/` (`run.json`, `fills|markouts|snapshots.jsonl`), `paper/basis/<TICKER>.json`,
`screener/*.parquet`, `backtests/*.parquet`, `charts/`, `logs/latest.log`.

## Подробнее

- `.claude/rules/model-and-timestamps.md` — модель исполнения, базис, задержки, временные шкалы, сопоставимость backtest/paper.
- `.claude/rules/deploy.md` — деплой paper-трейдера на VPS и последнее известное состояние сервера.
- `.claude/rules/research-history.md` — результаты прошлых бэктестов и выбор монет.
