#!/bin/sh
# The Tailscale Funnel sidecar: the official image's containerboot, started without anyone at a
# terminal. It idles until the setup wizard has written the auth key, logs in with the wizard's
# machine name, publishes this node's DNS name for the brain (its public MCP URL), and logs in
# again when the wizard saves a new key or name. Files in /brain (data/tailscale/ on the host):
#   authkey   written by the wizard; used only while not logged in (TS_AUTH_ONCE)
#   hostname  written by the wizard; the machine name (default company-brain)
#   dnsname   written here: <hostname>.<tailnet>.ts.net, read by the brain
#   state/    tailscaled's state: the login survives restarts
set -u
dir=/brain
boot=

nap() { sleep "$1" & wait $!; }  # a signal ends the nap, not only the sleep
inputs() { cat "$dir/authkey" "$dir/hostname" 2>/dev/null | md5sum; }

publish() {
  # Self comes before Peer in the status JSON, so the first DNSName is this node's.
  name=$(tailscale --socket=/tmp/tailscaled.sock status --json 2>/dev/null | grep -m1 '"DNSName"' | sed -n 's/.*"DNSName": *"\([^"]*\)\.".*/\1/p')
  if [ -n "$name" ] && [ "$name" != "$(cat "$dir/dnsname" 2>/dev/null)" ]; then
    echo "$name" > "$dir/.dnsname" && mv "$dir/.dnsname" "$dir/dnsname"
    echo "public URL: https://$name/mcp"
  fi
}

stop() {
  if [ -n "$boot" ]; then kill "$boot" 2>/dev/null; wait "$boot"; fi
  exit 0
}
trap stop TERM INT

[ -s "$dir/authkey" ] || echo "waiting for the Tailscale auth key from the setup wizard"
while :; do
  until [ -s "$dir/authkey" ]; do nap 5; done
  seen=$(inputs)
  TS_HOSTNAME=$(cat "$dir/hostname" 2>/dev/null || echo company-brain) containerboot &
  boot=$!
  while kill -0 "$boot" 2>/dev/null && [ "$(inputs)" = "$seen" ]; do
    publish
    nap 5
  done
  if kill -0 "$boot" 2>/dev/null; then
    echo "new auth key or machine name: logging in again"
    kill "$boot"
  else
    echo "tailscale stopped (a wrong or used auth key?): retrying in a minute or on a new key"
    nap 60
  fi
  wait "$boot"
  boot=
done
