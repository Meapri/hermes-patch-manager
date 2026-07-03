#!/usr/bin/env bash
# hermes modification-recovery installer.
#
# Deploys the recovery engine outside Hermes and wires systemd (user scope, to
# match a `systemctl --user` gateway) so mods are re-applied after `hermes
# update` (git-reset) wipes the core tree.
#
#   ./install.sh
# Env: GATEWAY_SERVICE HERMES_HOME HERMES_VENV_PYTHON HEALTH_PORT HEALTH_BIND MODE
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_SERVICE="${GATEWAY_SERVICE:-hermes-gateway.service}"
HEALTH_PORT="${HEALTH_PORT:-8577}"
HEALTH_BIND="${HEALTH_BIND:-auto}"

MODE="${MODE:-}"
if [ -z "$MODE" ]; then
  if systemctl --user cat "$GATEWAY_SERVICE" >/dev/null 2>&1; then MODE=user
  elif systemctl cat "$GATEWAY_SERVICE" >/dev/null 2>&1; then MODE=system
  else echo "[X] $GATEWAY_SERVICE not found; set GATEWAY_SERVICE/MODE"; exit 1; fi
fi

if [ "$MODE" = system ]; then
  [ "$(id -u)" -eq 0 ] || { echo "[X] system mode needs root"; exit 1; }
  DEST=/opt/hermes-recovery; BIN=/usr/local/bin; UNIT_DIR=/etc/systemd/system
  SCTL=(systemctl); WANTED=multi-user.target
  HERMES_HOME="${HERMES_HOME:-$(getent passwd "$(systemctl show -p User --value "$GATEWAY_SERVICE")" | cut -d: -f6)/.hermes}"
else
  DEST="$HOME/.local/share/hermes-recovery"; BIN="$HOME/.local/bin"; UNIT_DIR="$HOME/.config/systemd/user"
  SCTL=(systemctl --user); WANTED=default.target
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
fi

AGENT_DIR="$HERMES_HOME/hermes-agent"
GITDIR="$AGENT_DIR/.git"
VENV_PYTHON="${HERMES_VENV_PYTHON:-$AGENT_DIR/venv/bin/python}"
[ -x "$VENV_PYTHON" ] || { echo "[X] venv python not found: $VENV_PYTHON"; exit 1; }
[ -d "$GITDIR" ] || { echo "[X] not a git repo: $AGENT_DIR"; exit 1; }
echo "scope=$MODE agent=$AGENT_DIR venv=$VENV_PYTHON dest=$DEST"

# --- deploy code (preserve mods.d + config) ---------------------------------
mkdir -p "$DEST/mods.d" "$BIN" "$UNIT_DIR"
cp -r "$SRC/hpm.py" "$SRC/recovery" "$DEST/"
chmod +x "$DEST/hpm.py"
ln -sf "$DEST/hpm.py" "$BIN/hpm"

# --- config (only write if absent, to preserve edits) -----------------------
if [ ! -f "$DEST/config.json" ]; then
cat > "$DEST/config.json" <<JSON
{
  "hermes_agent_dir": "$AGENT_DIR",
  "venv_python": "$VENV_PYTHON",
  "mods_dir": "$DEST/mods.d",
  "mode": "$MODE",
  "restart_services": ["$GATEWAY_SERVICE"],
  "health_bind": "$HEALTH_BIND",
  "health_port": $HEALTH_PORT,
  "llm_merge": false
}
JSON
  echo "wrote config.json"
else
  echo "kept existing config.json"
fi

# --- systemd units ----------------------------------------------------------
cat > "$UNIT_DIR/hermes-recovery-health.service" <<UNIT
[Unit]
Description=hermes modification-recovery tailnet health endpoint
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=$DEST/hpm.py serve
Restart=always
RestartSec=5
[Install]
WantedBy=$WANTED
UNIT

cat > "$UNIT_DIR/hermes-recovery-heal.service" <<UNIT
[Unit]
Description=hermes modification-recovery heal (re-apply mods after update)
[Service]
Type=oneshot
# Let the git-reset + pip settle before re-applying source patches.
ExecStartPre=/bin/sleep 15
ExecStart=$DEST/hpm.py heal
UNIT

cat > "$UNIT_DIR/hermes-recovery-watch.path" <<UNIT
[Unit]
Description=watch hermes-agent reflog; heal when the tree is updated/reset
[Path]
# reflog is appended on every ref move (reset/pull/checkout) — reliable signal
# that \`hermes update\` touched the tree (unlike .git/HEAD, unchanged by reset).
PathModified=$GITDIR/logs/HEAD
Unit=hermes-recovery-heal.service
[Install]
WantedBy=$WANTED
UNIT

cat > "$UNIT_DIR/hermes-recovery-guard.service" <<UNIT
[Unit]
Description=hermes modification-recovery periodic guard
[Service]
Type=oneshot
ExecStart=$DEST/hpm.py heal
UNIT

cat > "$UNIT_DIR/hermes-recovery-guard.timer" <<UNIT
[Unit]
Description=hermes modification-recovery guard timer (backstop)
[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# --- activate ---------------------------------------------------------------
"${SCTL[@]}" daemon-reload
"${SCTL[@]}" enable --now hermes-recovery-health.service
"${SCTL[@]}" enable --now hermes-recovery-watch.path
"${SCTL[@]}" enable --now hermes-recovery-guard.timer

if [ "$MODE" = user ] && command -v loginctl >/dev/null; then
  loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes \
    || sudo loginctl enable-linger "$USER" 2>/dev/null || echo "  [!] run: sudo loginctl enable-linger $USER"
fi

echo; "$DEST/hpm.py" doctor || true
echo; echo "watch: ${SCTL[*]} status hermes-recovery-watch.path"
echo "register a mod:  hpm capture <id> --files a,b --new-files c"
