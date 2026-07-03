#!/usr/bin/env bash
# hermes-patch-manager installer (Ubuntu / systemd).
#
# Auto-detects whether the Hermes gateway is a SYSTEM service (root, units under
# /etc/systemd/system) or a `systemctl --user` service (per-user units), and
# installs the manager in the matching scope so the drift-guard can actually
# restart the gateway.
#
#   ./install.sh                 # auto-detect scope
#   MODE=user ./install.sh       # force per-user scope
#   MODE=system sudo ./install.sh
#
# Env overrides: GATEWAY_SERVICE HERMES_USER HERMES_HOME HERMES_VENV_PYTHON
#                PATCH_SRC HEALTH_PORT HEALTH_BIND
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_SERVICE="${GATEWAY_SERVICE:-hermes-gateway.service}"
HEALTH_PORT="${HEALTH_PORT:-8577}"
HEALTH_BIND="${HEALTH_BIND:-auto}"

# --- detect scope (user vs system) ------------------------------------------
MODE="${MODE:-}"
if [ -z "$MODE" ]; then
  if systemctl --user cat "$GATEWAY_SERVICE" >/dev/null 2>&1; then MODE=user
  elif systemctl cat "$GATEWAY_SERVICE" >/dev/null 2>&1; then MODE=system
  else echo "[X] $GATEWAY_SERVICE not found as a --user or system unit; set GATEWAY_SERVICE/MODE"; exit 1; fi
fi
echo "scope: $MODE   gateway: $GATEWAY_SERVICE"

if [ "$MODE" = system ]; then
  [ "$(id -u)" -eq 0 ] || { echo "[X] system mode needs root: sudo ./install.sh"; exit 1; }
  DEST=/opt/hermes-patch-manager;  BIN=/usr/local/bin
  UNIT_DIR=/etc/systemd/system;    SCTL=(systemctl);          WANTED=multi-user.target
  HERMES_USER="${HERMES_USER:-$(systemctl show -p User --value "$GATEWAY_SERVICE" 2>/dev/null || true)}"
  if [ -z "$HERMES_USER" ]; then shopt -s nullglob; h=(/home/*/.hermes); [ "${#h[@]}" -eq 1 ] && HERMES_USER="$(stat -c %U "${h[0]}")"; fi
  [ -n "$HERMES_USER" ] || { echo "[X] set HERMES_USER"; exit 1; }
  USER_HOME="$(getent passwd "$HERMES_USER" | cut -d: -f6)"
else
  DEST="$HOME/.local/share/hermes-patch-manager";  BIN="$HOME/.local/bin"
  UNIT_DIR="$HOME/.config/systemd/user";           SCTL=(systemctl --user);   WANTED=default.target
  HERMES_USER="$USER";  USER_HOME="$HOME"
fi

HERMES_HOME="${HERMES_HOME:-$USER_HOME/.hermes}"
AGENT_DIR="$HERMES_HOME/hermes-agent"
VENV_PYTHON="${HERMES_VENV_PYTHON:-}"
[ -n "$VENV_PYTHON" ] || for c in "$AGENT_DIR/venv/bin/python" "$AGENT_DIR/.venv/bin/python"; do [ -x "$c" ] && VENV_PYTHON="$c" && break; done
[ -x "${VENV_PYTHON:-/nonexistent}" ] || { echo "[X] venv python not found under $AGENT_DIR; set HERMES_VENV_PYTHON"; exit 1; }
echo "user=$HERMES_USER home=$HERMES_HOME venv=$VENV_PYTHON dest=$DEST"

# --- deploy the manager ------------------------------------------------------
mkdir -p "$DEST/patches" "$BIN" "$UNIT_DIR"
cp -r "$SRC/loader" "$SRC/registry.d" "$SRC/hpm.py" "$DEST/"
chmod +x "$DEST/hpm.py"
ln -sf "$DEST/hpm.py" "$BIN/hpm"

for c in "${PATCH_SRC:-}" "$SRC/patches/anthropic_billing_bypass.py" \
         "$SRC/../hermes-claude-auth/anthropic_billing_bypass.py" \
         "$HERMES_HOME/patches/anthropic_billing_bypass.py"; do
  if [ -n "$c" ] && [ -f "$c" ]; then cp "$c" "$DEST/patches/anthropic_billing_bypass.py"; echo "  seeded claude-auth <- $c"; break; fi
done
[ -f "$DEST/patches/anthropic_billing_bypass.py" ] || echo "  [!] claude-auth module not seeded; register later with 'hpm add ...'"

# --- config ------------------------------------------------------------------
cat > "$DEST/config.json" <<JSON
{
  "mode": "$MODE",
  "hermes_agent_dir": "$AGENT_DIR",
  "venv_python": "$VENV_PYTHON",
  "home": "$USER_HOME",
  "run_as_user": "",
  "gateway_service": "$GATEWAY_SERVICE",
  "health_bind": "$HEALTH_BIND",
  "health_port": $HEALTH_PORT,
  "auto_restart_on_drift": true
}
JSON

# --- gateway drop-in: PYTHONPATH injection (survives `hermes update`) ---------
DROPIN_DIR="$UNIT_DIR/${GATEWAY_SERVICE}.d"; mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/10-hermes-patch-manager.conf" <<CONF
# hermes-patch-manager: inject the out-of-venv loader so patches survive
# \`hermes update\` rebuilding the venv. ExecStartPre '-' never blocks startup.
[Service]
Environment=PYTHONPATH=$DEST/loader
ExecStartPre=-$DEST/hpm.py heal
CONF

# --- units -------------------------------------------------------------------
cat > "$UNIT_DIR/hermes-patch-health.service" <<UNIT
[Unit]
Description=hermes-patch-manager tailnet health endpoint
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=$DEST/hpm.py serve
Restart=always
RestartSec=5
[Install]
WantedBy=$WANTED
UNIT

cat > "$UNIT_DIR/hermes-patch-guard.service" <<UNIT
[Unit]
Description=hermes-patch-manager drift guard
[Service]
Type=oneshot
ExecStart=$DEST/hpm.py guard
UNIT

cat > "$UNIT_DIR/hermes-patch-guard.timer" <<UNIT
[Unit]
Description=hermes-patch-manager drift guard timer
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
AccuracySec=30s
Persistent=true
[Install]
WantedBy=timers.target
UNIT

# --- activate ----------------------------------------------------------------
"${SCTL[@]}" daemon-reload
"${SCTL[@]}" enable --now hermes-patch-health.service
"${SCTL[@]}" enable --now hermes-patch-guard.timer
echo "Restarting $GATEWAY_SERVICE to attach patches..."
"${SCTL[@]}" restart "$GATEWAY_SERVICE" || echo "  [!] restart $GATEWAY_SERVICE manually"

if [ "$MODE" = user ]; then
  # let user services run at boot without an active login (headless server)
  if command -v loginctl >/dev/null && ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes; then
    sudo loginctl enable-linger "$USER" 2>/dev/null && echo "enabled linger for $USER" || echo "  [!] run: sudo loginctl enable-linger $USER  (for boot persistence)"
  fi
fi

echo; "$DEST/hpm.py" doctor || true
echo; echo "hpm on PATH? -> $BIN/hpm  (add $BIN to PATH if needed)"
echo "health: ${SCTL[*]} status hermes-patch-health.service"
