#!/bin/sh
set -eu

# Require BACKEND_ORIGIN to be set (no defaults here)
if [ -z "${BACKEND_ORIGIN:-}" ]; then
  echo "ERROR: BACKEND_ORIGIN is not set. Set it via environment (e.g., docker-compose env file)." >&2
  exit 1
fi

# Render nginx config from template with env vars
envsubst '${BACKEND_ORIGIN}' < /etc/nginx/conf.d/default.conf.template > /etc/nginx/conf.d/default.conf

exec "$@"


