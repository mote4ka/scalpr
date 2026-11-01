---
paths:
  - "deploy/**"
---

# Деплой paper-трейдера

**Любые действия на сервере — только с явного разрешения пользователя.** Доступ — SSH-ключи; пароли из старых
переписок не использовать.

## Что делают скрипты (проверено по коду)

- `sh deploy/deploy.sh root@<server>` — `git archive HEAD` (только закоммиченное, незакоммиченное остаётся локально)
  по ssh → `deploy/server.sh` на сервере.
- `server.sh` (идемпотентный, Debian, от root): ставит `chrony curl ca-certificates tmux`, создаёт системного
  пользователя `scalpr` (home `/opt/scalpr`, без shell), ставит `uv`, останавливает сервис, заменяет
  `packages/` и `deploy/`, `uv sync --frozen --no-dev --package scalpr-trader`; `data/`, `.venv`, `trader.toml`
  остаются. `trader.toml` создаётся из `config.example.toml` только если его нет.
- `scalpr-trader.service`: `trader --config /opt/scalpr/trader.toml --record --dashboard-file /opt/scalpr/data/dashboard.txt`,
  `KillSignal=SIGINT` (журнал и записи закрываются корректно), `Restart=always`.
- `scalpr-cleanup.timer` (daily) удаляет дни `data/stream/<exchange>/<day>` старше 14 дней.
- `scalpr-watch` (`deploy/watch.sh`) — tmux: dashboard (`watch -c cat dashboard.txt`), `journalctl`, shell.
  Выход из tmux сервис не останавливает.
- `deploy/harden.sh` (отдельно от деплоя, идемпотентный): `ssh root@<server> 'sh -s' < deploy/harden.sh` —
  fail2ban для ssh (journal, бан через nftables, 1 ч с ростом до недели), unattended-upgrades (умолчания Debian,
  без перезагрузок), nftables-таблица `inet scalpr`: входящие только ssh, исходящие без ограничений.
  `sh -s -- --no-password` — ещё и вход только по ключу (`/etc/ssh/sshd_config.d/00-scalpr.conf`, перекрывает
  `50-cloud-init.conf`); перед этим в `authorized_keys` должны быть ключи всех устройств, включая Termius.
  Новые правила firewall откатываются через 5 мин, если не подтвердить новым соединением
  `ssh root@<server> systemctl stop scalpr-fw-rollback.timer`. При первом включении открытые соединения на секунды
  теряют пакеты: ленты трейдера замолкают, HTX переподключается через 30 с.
- swap в скриптах нет (настроен вручную).

## Состояние сервера (проверено 2026-10-31, read-only)

- Debian, KVM, 1 vCPU AMD EPYC, ~1 GB RAM + 1 GB swap-файл, часовой пояс MSK (journalctl в MSK, данные в UTC).
  Адрес сервера — в личной памяти Claude, не в репозитории. Провайдер/регион по коду не подтверждены
  (в старой переписке — DigitalOcean SGP1).
- Прогон 2026-10-27 17:40 → 2026-11-01 09:35 UTC (журнал `2026-10-27_174038`, без skew): +$29.99, 314 fills.
- С 2026-11-01 09:35 UTC работает код `4111350` с `skew = 0.1` (журнал `2026-11-01_093546`); прежний конфиг —
  `/opt/scalpr/trader.toml.bak-2026-11-01`. Трейдер ~4% CPU, ~130–160 MB RSS.
- `trader.toml` на сервере: DOT edge 0.25, AVAX edge 0.2, `quote_latency = 100ms`, `hedge_latency = 20ms`
  (receive-шкала, см. `model-and-timestamps.md`), `skew = 0.1`, остальное как в примере.
- Рестарт сервиса = новый журнал и обнулённая paper-позиция; базис подхватывается из `data/paper/basis`.
- На сервере uv 0.13.x, а пакеты требуют `uv_build>=0.12.5,<0.13` — пока только предупреждение при сборке.
- Сравнение с прежним конфигом без второго трейдера: скачать записи начиная с файла рестарта
  (`data/stream/<exchange>/<day>/<HH>_<MMSS>.jsonl.gz`, там snapshot) и прогнать `trader --replay` со `skew = 0`.
- Записи потока ~150–225 MB/день на 2 монеты (в дни обвала больше).
- Задержка лент (receive − время биржи, chrony-синхронизация): Bybit книга p50 ~8 ms, HTX книга/сделки p50 ~37 ms.
- **Замирания процесса/сети:** примерно раз в час обе ленты одновременно опаздывают на 5–8 s (причина не найдена:
  своп/PSI/steal в норме). Отдельно бывают задержки только HTX до ~6.5 s. `stale = 10s` их не ловит.
- Состояние защиты сервера — в личной памяти Claude, не в репозитории (он публичный).
