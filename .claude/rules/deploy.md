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
- В скриптах **нет** swap, firewall, fail2ban, unattended-upgrades — если они есть, настроены вручную.

## Последнее известное состояние (на 2026-10-27, не проверено с тех пор)

- DigitalOcean SGP1, Debian 13, 1 vCPU / 512 MB; нагрузка трейдера ~2.5% CPU, ~130 MB RAM.
- Монеты DOT и AVAX; на сервере в `trader.toml` ставили `quote_latency = 100ms`, `hedge_latency = 20ms`
  (receive-шкала, см. `model-and-timestamps.md`), в примере в репо — 50/50 ms.
- Записи потока ~165 MB/день на 2 монеты (до ~700 MB/день на 5).
- Измерения из SGP1: Bybit feed ~9 ms one-way, HTX feed ~48 ms (p90 ~130 ms), Bybit RTT ~16–17 ms, HTX RTT ~83–106 ms.

Жив ли сервер, запущен ли сервис, какой там конфиг и сколько накопилось журналов — неизвестно, проверять заново.
