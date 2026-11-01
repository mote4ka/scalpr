#!/bin/sh
# HARDEN a Debian server, as root: fail2ban for ssh, a firewall that lets in only ssh, automatic security updates;
# with --no-password also key-only ssh (first add every key you log in with to /root/.ssh/authorized_keys)
# safe to run again; from the repo, with ssh key access:
#   ssh root@<server> 'sh -s' < deploy/harden.sh
#   ssh root@<server> 'sh -s -- --no-password' < deploy/harden.sh
# new firewall rules roll themselves back in 5 minutes unless confirmed over a new ssh connection:
#   ssh root@<server> systemctl stop scalpr-fw-rollback.timer
set -eu
no_password=false
[ "${1:-}" = "--no-password" ] && no_password=true

missing=""
for package in fail2ban python3-systemd nftables unattended-upgrades; do
    dpkg -s "$package" >/dev/null 2>&1 || missing="$missing $package"
done
if [ -n "$missing" ]; then
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $missing
fi

# fail2ban reads sshd from the journal (Debian has no auth.log) and bans through nftables
cat > /etc/fail2ban/jail.d/scalpr.local <<'EOF'
# scalpr (deploy/harden.sh): ban for an hour, longer for whoever comes back
[DEFAULT]
banaction = nftables
banaction_allports = nftables[type=allports]
bantime = 1h
bantime.increment = true
bantime.maxtime = 1w
findtime = 10m
maxretry = 5

[sshd]
enabled = true
backend = systemd
EOF
systemctl enable fail2ban
systemctl restart fail2ban
for _ in 1 2 3 4 5 6 7 8 9 10; do
    fail2ban-client ping >/dev/null 2>&1 && break
    sleep 1
done

# Debian's default origins (security fixes and stable point releases), never an automatic reboot
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# keys only; sshd keeps the first value it reads, so this file goes before cloud-init's 50-cloud-init.conf
if $no_password; then
    if ! ssh-keygen -lf /root/.ssh/authorized_keys >/dev/null 2>&1; then
        echo "no keys in /root/.ssh/authorized_keys, passwords stay on" >&2
        exit 1
    fi
    cat > /etc/ssh/sshd_config.d/00-scalpr.conf <<'EOF'
# scalpr (deploy/harden.sh): keys only
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    if ! sshd -t; then
        rm -f /etc/ssh/sshd_config.d/00-scalpr.conf
        exit 1
    fi
    systemctl reload ssh
fi

# only ssh comes in; everything the trader needs goes out (exchanges, DNS, NTP) and its replies come back.
# The file replaces only its own table: fail2ban's table stays (Debian's default file flushes everything).
# The first time, open connections are unknown to the firewall until they send something: the trader's
# feeds go quiet for a few seconds and the HTX one reconnects after its 30 s timeout (seen 2026-11-01)
port=$(sshd -T | awk '$1 == "port" {print $2; exit}')
rules=$(mktemp)
cat > "$rules" <<EOF
#!/usr/sbin/nft -f
# scalpr (deploy/harden.sh): only ssh comes in
table inet scalpr
delete table inet scalpr
table inet scalpr {
	chain input {
		type filter hook input priority filter; policy drop;
		ct state established,related accept
		ct state invalid drop
		iif lo accept
		meta l4proto { icmp, ipv6-icmp } accept
		tcp dport $port accept
	}
	chain forward {
		type filter hook forward priority filter; policy drop;
	}
}
EOF
if cmp -s "$rules" /etc/nftables.conf && nft list table inet scalpr >/dev/null 2>&1; then
    rm -f "$rules"
else
    [ -f /etc/nftables.conf.orig ] || cp /etc/nftables.conf /etc/nftables.conf.orig
    install -m 755 "$rules" /etc/nftables.conf
    rm -f "$rules"
    # if the new rules lock ssh out, they go away by themselves
    systemctl stop scalpr-fw-rollback.timer 2>/dev/null || true
    systemd-run --quiet --unit=scalpr-fw-rollback --on-active=5min /bin/sh -c 'nft delete table inet scalpr; systemctl disable nftables'
    systemctl enable nftables
    systemctl reload-or-restart nftables
    echo "firewall on; it rolls back in 5 minutes unless confirmed over a new ssh connection:"
    echo "  ssh root@<server> systemctl stop scalpr-fw-rollback.timer"
fi

sshd -T | grep -E '^(passwordauthentication|permitrootlogin) '
fail2ban-client status sshd | grep -E 'Total (failed|banned)'
