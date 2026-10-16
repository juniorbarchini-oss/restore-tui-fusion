#!/usr/bin/env bash
# ==============================================================================
# restore-tui - Bare-Metal Disaster Recovery
# Generated automatically during backup. Run this from inside the snapshot folder.
# ==============================================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/_system_state"
HOME_SOURCE="${SCRIPT_DIR}/home"

# Lines for the "software to install" checklist printed at the end - not an
# error report. Some things (Chrome/Tailscale-style logins, Ubuntu Pro/ESM
# packages, apps installed from a standalone .deb, snaps, this app's own
# /opt install) can't be safely reinstalled unattended, so instead of guessing
# we just hand back a clean shopping list, same idea as reinstalling on a Mac
# and grabbing your apps again from the App Store.
TODO=()

echo -e "${GREEN}=======================================================${NC}"
echo -e "${GREEN}      restore-tui - BARE-METAL DISASTER RESTORE         ${NC}"
echo -e "${GREEN}=======================================================${NC}"
echo -e "Snapshot: ${SCRIPT_DIR}"
echo -e "Target home: ${HOME}"
echo ""
read -r -p "Perform a full system restore from this snapshot? (yes/no): " CONFIRM
[[ "${CONFIRM}" == "yes" ]] || { echo -e "${YELLOW}Aborted.${NC}"; exit 0; }

