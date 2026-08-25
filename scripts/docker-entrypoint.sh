#!/bin/sh
# Standalone Docker entrypoint for mammamiradio.
#
# Mirrors the HA add-on pattern: if ADMIN_TOKEN is unset, auto-generate one
# and persist it to /data/admin_token so it survives container restarts.
# Operators can read the value with:
#   docker compose exec mammamiradio cat /data/admin_token
#
# HA add-on mode uses ha-addon/mammamiradio/rootfs/run.sh instead — this
# entrypoint is only for the standalone Dockerfile.

set -e

TOKEN_FILE="${MAMMAMIRADIO_ADMIN_TOKEN_FILE:-/data/admin_token}"

if [ -z "${ADMIN_TOKEN:-}" ]; then
    if [ -f "$TOKEN_FILE" ]; then
        ADMIN_TOKEN="$(cat "$TOKEN_FILE")"
        echo "[mammamiradio] Loaded ADMIN_TOKEN from $TOKEN_FILE" >&2
    else
        # Generate 32 hex chars (128 bits). /dev/urandom is the portable source
        # — openssl is not in the slim base image and adding it would bloat the
        # image for one line of randomness.
        ADMIN_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        if [ -w "$(dirname "$TOKEN_FILE")" ]; then
            printf '%s' "$ADMIN_TOKEN" > "$TOKEN_FILE"
            chmod 600 "$TOKEN_FILE" 2>/dev/null || true
            echo "[mammamiradio] Generated ADMIN_TOKEN and persisted to $TOKEN_FILE" >&2
            echo "[mammamiradio] Read it with: docker compose exec <service> cat $TOKEN_FILE" >&2
        else
            # /data is not writable — the token cannot be persisted and will
            # regenerate on the next start. Never print the value: it would
            # land in Docker logs.
            echo "[mammamiradio] WARNING: $TOKEN_FILE not writable; ADMIN_TOKEN will regenerate on restart" >&2
            echo "[mammamiradio] Set ADMIN_TOKEN in the environment; the generated value is not shown" >&2
        fi
    fi
    export ADMIN_TOKEN
fi

exec "$@"
