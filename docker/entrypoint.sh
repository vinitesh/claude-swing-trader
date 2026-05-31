#!/bin/sh
# entrypoint.sh — ensures runtime dirs exist and are writable, then exec's swingbot.
#
# We run as a non-root user (UID 1000 / `swing`) for safety, but bind mounts can
# come in with arbitrary host ownership. Rather than fight UIDs, we just verify
# we can write to the data dirs and fail loudly if not — that gives a clearer
# signal than a stack trace deep in loguru.
set -e

for dir in /app/logs /app/data_cache /app/backtest_results /app/state; do
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir" 2>/dev/null || {
            echo "ERROR: cannot create $dir — check volume mount and host permissions" >&2
            exit 1
        }
    fi
    if ! touch "$dir/.write-test" 2>/dev/null; then
        echo "ERROR: $dir is not writable by container user (uid=$(id -u))." >&2
        echo "Fix: on the host, run 'chmod -R a+w \$(pwd)/$(basename "$dir")'" >&2
        echo "or chown to the container UID." >&2
        exit 1
    fi
    rm -f "$dir/.write-test"
done

# trading.db: the file may be touched on the host; make sure we can open it RW.
if [ -e /app/trading.db ] && [ ! -w /app/trading.db ]; then
    echo "ERROR: /app/trading.db is not writable by container user (uid=$(id -u))." >&2
    echo "Fix on the host: chmod a+w trading.db" >&2
    exit 1
fi

exec swingbot "$@"
