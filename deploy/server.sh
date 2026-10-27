#!/bin/sh
# server side of deploy.sh, as root on Debian: packages, the scalpr user, uv, the code and its venv, systemd units
# safe to run again: every deploy goes through it
set -eu
code=$1  # the unpacked archive
root=/opt/scalpr

missing=""
for package in chrony curl ca-certificates; do
    dpkg -s "$package" >/dev/null 2>&1 || missing="$missing $package"
done
if [ -n "$missing" ]; then
    apt-get update -q
    apt-get install -y -q $missing
fi

# the trader runs as its own user, without a password or a shell; its home is the workspace
id scalpr >/dev/null 2>&1 || useradd --system --create-home --home-dir "$root" --shell /usr/sbin/nologin scalpr
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

systemctl stop scalpr-trader 2>/dev/null || true
# the code is replaced whole; data/, .venv and trader.toml stay
rm -rf "$root/packages" "$root/deploy"
tar -C "$code" -c . | runuser -u scalpr -- tar -x -C "$root"
rm -rf "$code"
cd "$root"
runuser -u scalpr -- env HOME="$root" uv sync --frozen --no-dev --package scalpr-trader
[ -f trader.toml ] || runuser -u scalpr -- cp packages/trader/config.example.toml trader.toml

install -m 644 deploy/*.service deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now scalpr-cleanup.timer
systemctl enable scalpr-trader
systemctl start scalpr-trader
echo "deployed; logs: journalctl -u scalpr-trader -f"
