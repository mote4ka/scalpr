#!/bin/sh
# WATCH: a tmux session to look at the trader on the server, installed as scalpr-watch by deploy/server.sh
# windows: the trader's dashboard, its log, a shell in /opt/scalpr; Ctrl+B then a window number to switch,
# Ctrl+B D to leave; the trader is a systemd service, nothing done here (Ctrl+C included) stops it
session=scalpr
export LANG=C.UTF-8
if ! tmux has-session -t "$session" 2>/dev/null; then
    tmux new-session -d -s "$session" -n dashboard "watch -c -t -n 1 cat /opt/scalpr/data/dashboard.txt"
    tmux new-window -t "$session" -n log "journalctl -u scalpr-trader -f -n 200"
    tmux new-window -t "$session" -n shell -c /opt/scalpr
    tmux select-window -t "$session:dashboard"
fi
exec tmux -u attach -t "$session"
