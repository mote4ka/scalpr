#!/bin/sh
# DEPLOY: the committed code to a Debian server, then (re)start the paper trader there
# from the repo, with ssh key access: sh deploy/deploy.sh root@<server>
# the server side is deploy/server.sh; the trader's config there is /opt/scalpr/trader.toml, made once from
# packages/trader/config.example.toml and never overwritten
set -eu
host=$1
cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain)" ]; then
    echo "uncommitted changes stay here, deploying $(git log -1 --format=%h)" >&2
fi
git archive --format=tar HEAD | ssh "$host" '
    rm -rf /tmp/scalpr-deploy && mkdir /tmp/scalpr-deploy && tar -x -C /tmp/scalpr-deploy &&
    sh /tmp/scalpr-deploy/deploy/server.sh /tmp/scalpr-deploy'
