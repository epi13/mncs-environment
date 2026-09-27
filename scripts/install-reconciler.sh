#!/usr/bin/env bash
# Install (or update) the user-level reconciler service.
#
#   scripts/install-reconciler.sh [--workspace DIR] [--state-dir DIR]
#       [--interval SECONDS] [--enable-now] [--restart]
#
# Writes ~/.config/systemd/user/mncs-environment-reconciler.service from
# the template with local paths, reloads the user daemon, and optionally
# enables/starts it. Update behavior: re-run after git pull, then
# --restart to pick up the new source revision.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="$HOME/Documents/Projects"
STATE_DIR="$HOME/.local/share/mncs-environment"
INTERVAL=60
ENABLE_NOW=0
RESTART=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace) WORKSPACE="$2"; shift 2;;
        --state-dir) STATE_DIR="$2"; shift 2;;
        --interval) INTERVAL="$2"; shift 2;;
        --enable-now) ENABLE_NOW=1; shift;;
        --restart) RESTART=1; shift;;
        *) echo "unknown argument: $1" >&2; exit 2;;
    esac
done

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed -e "s|@REPO@|$REPO|g" \
    -e "s|@STATE_DIR@|$STATE_DIR|g" \
    -e "s|@WORKSPACE@|$WORKSPACE|g" \
    -e "s|@INTERVAL@|$INTERVAL|g" \
    "$REPO/deploy/mncs-environment-reconciler.service.template" \
    > "$UNIT_DIR/mncs-environment-reconciler.service"

systemctl --user daemon-reload

if [[ "$ENABLE_NOW" == 1 ]]; then
    systemctl --user enable --now mncs-environment-reconciler.service
fi
if [[ "$RESTART" == 1 ]]; then
    systemctl --user restart mncs-environment-reconciler.service
fi

if [[ "$(loginctl show-user "$USER" 2>/dev/null | grep -o 'Linger=.*')" != "Linger=yes" ]]; then
    echo "note: user lingering is off; the service stops at logout." >&2
    echo "run 'loginctl enable-linger $USER' to survive logout." >&2
fi

systemctl --user status mncs-environment-reconciler.service --no-pager --lines 3 || true
echo "installed $UNIT_DIR/mncs-environment-reconciler.service"
