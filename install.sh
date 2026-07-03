#!/usr/bin/env bash
# hermes-patch-manager installer (Ubuntu / systemd system, root).
#
#   sudo ./install.sh
#
# Overridable via env:
#   GATEWAY_SERVICE   (default: hermes-gateway.service)
#   HERMES_USER       (default: auto-detected from the gateway unit or /home/*/.hermes)
#   HERMES_HOME       (default: <user home>/.hermes)
#   HERMES_VENV_PYTHON(default: <home>/.hermes/hermes-agent/venv/bin/python)
#   PATCH_SRC         path to anthropic_billing_bypass.py to seed the claude-auth patch
#   HEALTH_PORT       (default: 8577)
#   HEALTH_BIND       (default: auto -> tailscale ip -4)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo ./install.sh"; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/hermes-patch-manager

GATEWAY_SERVICE="${GATEWAY_SERVICE:-hermes-gateway.service}"
HEALTH_PORT="${HEALTH_PORT:-8577}"
HEALTH_BIND="${HEALTH_BIND:-auto}"

# --- resolve hermes identity -------------------------------------------------
HERMES_USER="${HERMES_USER:-}"
if [ -z "$HERMES_USER" ]; then
  HERMES_USER="$(systemctl show -p User --value "$GATEWAY_SERVICE" 2>/dev/null || true)"
fi
if [ -z "$HERMES_USER" ]; then
  shopt -s nullglob
  homes=(/home/*/.hermes)
  [ "${#homes[@]}" -eq 1 ] && HERMES_USER="$(stat -c %U "${homes[0]}")"
fi
[ -n "$HERMES_USER" ] || { echo "[X] cannot detect hermes user; set HERMES_USER=..."; exit 1; }

USER_HOME="$(getent passwd "$HERMES_USER" | cut -d: -f6)"
HERMES_HOME="${HERMES_HOME:-$USER_HOME/.hermes}"
AGENT_DIR="$HERMES_HOME/hermes-agent"

VENV_PYTHON="${HERMES_VENV_PYTHON:-}"
if [ -z "$VENV_PYTHON" ]; then
  for c in "$AGENT_DIR/venv/bin/python" "$AGENT_DIR/.venv/bin/python"; do
    [ -x "$c" ] && VENV_PYTHON="$c" && break
  done
fi
[ -x "${VENV_PYTHON:-/nonexistent}" ] || { echo "[X] venv python not found under $AGENT_DIR; set HERMES_VENV_PYTHON"; exit 1; }

echo "Resolved: user=$HERMES_USER home=$HERMES_HOME venv=$VENV_PYTHON gateway=$GATEWAY_SERVICE"

# --- deploy the manager ------------------------------------------------------
mkdir -p "$DEST/patches"
cp -r "$SRC/loader" "$SRC/registry.d" "$SRC/hpm.py" "$DEST/"
chmod +x "$DEST/hpm.py"
ln -sf "$DEST/hpm.py" /usr/local/bin/hpm

# seed the claude-auth patch module if we can find it
for c in "${PATCH_SRC:-}" \
         "$SRC/patches/anthropic_billing_bypass.py" \
         "$SRC/../hermes-claude-auth/anthropic_billing_bypass.py" \
         "$HERMES_HOME/patches/anthropic_billing_bypass.py"; do
  if [ -n "$c" ] && [ -f "$c" ]; then
    cp "$c" "$DEST/patches/anthropic_billing_bypass.py"
    echo "  seeded claude-auth module <- $c"; break
  fi
done
if [ ! -f "$DEST/patches/anthropic_billing_bypass.py" ]; then
  echo "  [!] claude-auth module not found. Register it later with:"
  echo "      hpm add /path/anthropic_billing_bypass.py --name claude-auth \\"
  echo "        --hook agent.error_classifier:_install_thinking_replay_classifier_patch \\"
  echo "        --hook agent.anthropic_adapter:apply_patches \\"
  echo "        --verify agent.anthropic_adapter:_CLAUDE_CODE_BYPASS_APPLIED"
fi

# --- config ------------------------------------------------------------------
cat > "$DEST/config.json" <<JSON
{
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

# --- gateway drop-in (PYTHONPATH injection -> survives hermes update) ---------
DROPIN_DIR="/etc/systemd/system/${GATEWAY_SERVICE}.d"
mkdir -p "$DROPIN_DIR"
cp "$SRC/systemd/hermes-gateway.service.d/10-hermes-patch-manager.conf" "$DROPIN_DIR/10-hermes-patch-manager.conf"

# --- units -------------------------------------------------------------------
cp "$SRC/systemd/hermes-patch-health.service" /etc/systemd/system/
cp "$SRC/systemd/hermes-patch-guard.service"  /etc/systemd/system/
cp "$SRC/systemd/hermes-patch-guard.timer"    /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now hermes-patch-health.service
systemctl enable --now hermes-patch-guard.timer
echo "Restarting $GATEWAY_SERVICE to attach patches..."
systemctl restart "$GATEWAY_SERVICE" || echo "  [!] restart $GATEWAY_SERVICE manually"

echo
"$DEST/hpm.py" doctor || true
echo
echo "Health endpoint: systemctl status hermes-patch-health.service   (bind: $HEALTH_BIND:$HEALTH_PORT)"
echo "Check anytime:   hpm check     |     hpm doctor     |     hpm status"
