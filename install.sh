#!/bin/bash
# Herdr bridge installer for macOS and Linux.
# Run: curl -fsSL https://raw.githubusercontent.com/kuldeepakkatwal/herdr-bridge/main/install.sh | bash
# Safe to run again: it updates the bridge and restarts it.
set -euo pipefail
BASE="${HERDR_BRIDGE_BASE:-https://raw.githubusercontent.com/kuldeepakkatwal/herdr-bridge/main}"
DIR="$HOME/.herdr-remote"; PORT=8795
say() { printf '\n%s\n' "$*"; }
fail() { say "✗ $*"; exit 1; }

case "$(uname)" in
  Darwin) OS=mac ;;
  Linux) OS=linux ;;
  *) fail "This installer is for macOS and Linux." ;;
esac

PY="$(command -v python3)" || fail "python3 is missing. Install it (on a Mac: xcode-select --install; on Linux: your package manager), then run this again."
TS="$(command -v tailscale || true)"
if [ -z "$TS" ] && [ "$OS" = mac ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi
[ -n "$TS" ] || fail "Tailscale is not installed. Get it from tailscale.com/download, sign in, then run this again."
command -v herdr >/dev/null || [ -x "$HOME/.local/bin/herdr" ] || fail "herdr is not installed on this computer. Install herdr first, then run this again."
if [ "$OS" = linux ]; then
  systemctl --user show-environment >/dev/null 2>&1 || fail "systemd user services are not available here (systemctl --user does not work). Log in with a normal desktop or SSH session, then run this again."
fi

say "Downloading the bridge…"
mkdir -p "$DIR"
for f in herdr_remote.py qrcodegen.py com.herdr-remote.bridge.plist; do
  curl -fsSL "$BASE/$f" -o "$DIR/$f.new" || fail "Could not download $f. Check the internet connection and run this again."
  mv "$DIR/$f.new" "$DIR/$f"
done

say "Checking who is signed in to Tailscale…"
STATUS="$("$TS" status --json 2>/dev/null)" || fail "Tailscale is not running or not signed in. Open Tailscale, sign in, then run this again."
OWNER="$(printf '%s' "$STATUS" | "$PY" "$DIR/herdr_remote.py" --owner-from-status)" || fail "Could not tell who is signed in to Tailscale. Sign in to Tailscale, then run this again."
HOST="$(printf '%s' "$STATUS" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

# Run once so the owner and port are saved to ~/.herdr-remote/config.json, then stop it.
"$PY" "$DIR/herdr_remote.py" --owner "$OWNER" --port "$PORT" >/dev/null 2>&1 & PID=$!; sleep 1; kill $PID 2>/dev/null || true

say "Starting the bridge (it will also start by itself after a restart)…"
if [ "$OS" = mac ]; then
  PLIST="$HOME/Library/LaunchAgents/com.herdr-remote.bridge.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s#__PYTHON__#$PY#" -e "s#__BRIDGE__#$DIR/herdr_remote.py#" -e "s#__HOME__#$HOME#g" "$DIR/com.herdr-remote.bridge.plist" > "$PLIST"
  launchctl bootout "gui/$(id -u)/com.herdr-remote.bridge" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
else
  UNITDIR="$HOME/.config/systemd/user"
  mkdir -p "$UNITDIR"
  cat > "$UNITDIR/herdr-remote.service" <<UNIT
[Unit]
Description=Herdr Remote bridge

[Service]
ExecStart=$PY $DIR/herdr_remote.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now herdr-remote
  systemctl --user restart herdr-remote   # picks up a newer bridge when it was already running
  loginctl enable-linger "$USER" 2>/dev/null || say "Note: the bridge runs while you are logged in to this computer."
fi

# Tailscale's output is shown live: if Serve is off for the account it prints a link and waits until it is approved.
serve() { "$TS" serve --bg --https=$PORT http://127.0.0.1:$PORT 2>&1 | tee /dev/stderr; return "${PIPESTATUS[0]}"; }
say "Publishing the bridge on your Tailscale network… (if Tailscale shows a link below, open it, approve, and this continues)"
if ! OUT="$(serve)"; then
  case "$OUT" in
    *ccess*denied*|*ermission*|*operator*)
      if [ "$OS" = linux ]; then
        say "Tailscale needs a one-time permission for your user. It will ask for your password once."
        sudo tailscale set --operator="$USER" || fail "Could not give your user permission. Run: sudo tailscale set --operator=\$USER, then run this again."
        OUT="$(serve)" || fail "Tailscale could not publish the bridge: $OUT"
      else
        fail "Tailscale could not publish the bridge: $OUT"
      fi ;;
    *HTTPS*|*https*|*cert*) fail "Turn on HTTPS Certificates at https://login.tailscale.com/admin/dns, then run this again." ;;
    *) fail "Tailscale could not publish the bridge: $OUT" ;;
  esac
fi

URL="https://$HOST:$PORT"
if [ "$OS" = mac ]; then NAME="$(scutil --get ComputerName 2>/dev/null || hostname)"; else NAME="$(hostname)"; fi
LINK="$("$PY" -c 'import sys,urllib.parse as u;q=lambda s:u.quote(s,safe="");print("herdr://add?url="+q(sys.argv[1])+"&name="+q(sys.argv[2])+"&kind="+q(sys.argv[3]))' "$URL" "$NAME" "$OS")"

say "✓ Herdr bridge is running for $OWNER."
"$PY" "$DIR/herdr_remote.py" --qr "$LINK" || say "(Could not draw the QR code here. Type the address below in Herdr instead.)"
say "Scan this with your iPhone (Camera app, or Scan in Herdr), then tap Connect."
say "Or type this address in Herdr: $URL"
say "Your iPhone must be signed in to Tailscale as $OWNER."
