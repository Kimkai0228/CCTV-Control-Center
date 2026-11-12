#!/bin/sh
# Start as root, align the app user with the host, then drop privileges.
set -e

APP_USER="${APP_USER:-cctv}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# If the container was started with an explicit non-root user, there is nothing
# to align; just run the command as-is.
if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

# Match the app user's primary UID/GID to the host owner of ./data.
if [ "$(id -u "$APP_USER")" != "$PUID" ]; then
    usermod -o -u "$PUID" "$APP_USER"
fi
if [ "$(id -g "$APP_USER")" != "$PGID" ]; then
    groupmod -o -g "$PGID" "$APP_USER"
fi

# The render/video group GIDs inside the container follow the host device
# nodes, which is what lets a non-root process use Intel QuickSync.
for node in /dev/dri/renderD*; do
    [ -e "$node" ] || continue
    groupmod -o -g "$(stat -c '%g' "$node")" render 2>/dev/null || true
    break
done
for node in /dev/dri/card*; do
    [ -e "$node" ] || continue
    groupmod -o -g "$(stat -c '%g' "$node")" video 2>/dev/null || true
    break
done

# Ensure the unprivileged user owns its writable state (SQLite, uploads, work).
if [ -d /app/data ]; then
    chown -R "$APP_USER:$APP_USER" /app/data 2>/dev/null || true
fi

exec gosu "$APP_USER" "$@"
