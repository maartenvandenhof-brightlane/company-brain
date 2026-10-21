#!/bin/sh
# Run the brain as the host user (HOST_UID/HOST_GID), so the bind-mounted data folder stays
# editable on the host (Obsidian, VS Code). Docker creates a missing bind folder as root: hand
# those to that user first (the folders only, never a recursive chown of the wiki).
set -eu
if [ "$(id -u)" = 0 ]; then
  uid="${HOST_UID:-1000}" gid="${HOST_GID:-1000}"
  for d in /data /data/wiki /data/tailscale; do
    if [ -d "$d" ] && [ "$(stat -c %u "$d")" = 0 ]; then chown "$uid:$gid" "$d"; fi
  done
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$0" "$@"
fi
mkdir -p /data/wiki /data/state /data/secrets /data/portal /data/tailscale
chmod 700 /data/secrets /data/tailscale
exec wiki-mcp "$@"
