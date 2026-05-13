#!/bin/sh
set -eu

export PIKOBOT_WORKSPACE="${PIKOBOT_WORKSPACE:-/root/.pikobot/workspace}"

mkdir -p "${PIKOBOT_WORKSPACE}"

python /app/bootstrap_env.py

exec pikobot "$@"
