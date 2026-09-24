#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$HOME/.local/share/gromacs-telegram"
CONFIG_DIR="$HOME/.config/gromacs-telegram"
STATE_DIR="$HOME/.local/state/gromacs-telegram"
SYSTEMD_DIR="$HOME/.config/systemd/user"
CONFIG_FILE="$CONFIG_DIR/config.env"
SERVICE_FILE="$SYSTEMD_DIR/gromacs-telegram.service"

for cmd in python3 pgrep kill systemctl; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "Required command not found: $cmd" >&2; exit 1; }
done

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR" "$SYSTEMD_DIR"
cp "$SCRIPT_DIR/gromacs_bot.py" "$APP_DIR/gromacs_bot.py"
rm -rf "$APP_DIR/gromacs_monitor"
cp -R "$SCRIPT_DIR/gromacs_monitor" "$APP_DIR/gromacs_monitor"
find "$APP_DIR/gromacs_monitor" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
cp "$SCRIPT_DIR/gromacs-telegram.service" "$SERVICE_FILE"

if [[ ! -f "$CONFIG_FILE" ]]; then
    cp "$SCRIPT_DIR/config.env.example" "$CONFIG_FILE"
fi
chmod 600 "$CONFIG_FILE"

GMX_PATH="$(awk -F= '$1=="GMX_PATH" {sub(/^[^=]*=/, ""); print; exit}' "$CONFIG_FILE")"
if [[ -z "$GMX_PATH" || ! -x "$GMX_PATH" ]]; then
    echo "Configured GMX_PATH is not executable: ${GMX_PATH:-<empty>}" >&2
    echo "Edit $CONFIG_FILE, then rerun ./install.sh" >&2
    exit 1
fi

systemctl --user daemon-reload
systemctl --user enable gromacs-telegram.service

TOKEN="$(awk -F= '$1=="TELEGRAM_BOT_TOKEN" {sub(/^[^=]*=/, ""); print; exit}' "$CONFIG_FILE")"
USER_ID="$(awk -F= '$1=="TELEGRAM_USER_ID" {sub(/^[^=]*=/, ""); print; exit}' "$CONFIG_FILE")"
if [[ -n "$TOKEN" && -n "$USER_ID" ]]; then
    systemctl --user restart gromacs-telegram.service
else
    echo "Telegram credentials are not filled yet; service installed and enabled but not started."
fi

if command -v loginctl >/dev/null 2>&1; then
    LINGER="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)"
    if [[ "$LINGER" != "yes" ]]; then
        echo
        echo "Optional for boot/logout persistence:"
        echo 'loginctl enable-linger "$USER"'
    fi
fi

echo
echo "Next steps:"
echo "1. Edit ~/.config/gromacs-telegram/config.env and fill TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID."
echo "2. Run: systemctl --user restart gromacs-telegram.service"
echo "3. Check: systemctl --user status gromacs-telegram.service"
echo "4. Logs: journalctl --user -u gromacs-telegram.service -f"
