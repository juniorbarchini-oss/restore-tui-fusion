#!/usr/bin/env bash
# ==============================================================================
# btrfs-restore-tui - Bare-Metal Disaster Recovery (Arch / Omarchy)
# Generated automatically during backup. Run from INSIDE this snapshot folder,
# on a freshly installed Arch base (booted, btrfs root, your user created), or
# from a live ISO with the new root mounted at /mnt (pass --root /mnt).
#
# Dependencies: bash, coreutils, rsync, pacman, btrfs-progs, zstd. Nothing else.
# ==============================================================================
set -euo pipefail

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/_system_state"
ROOT_PREFIX=""
DO_HOME=1; DO_PKGS=1; DO_ETC=1

while [ $# -gt 0 ]; do
    case "$1" in
        --root) ROOT_PREFIX="${2%/}"; shift 2 ;;
        --no-home) DO_HOME=0; shift ;;
        --no-packages) DO_PKGS=0; shift ;;
        --no-etc) DO_ETC=0; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
done

# The home-tree step (rsync of root-owned files, chown -R) needs root. When we
# are restoring the booted system (no --root) and were not started with sudo,
# re-exec under sudo so [5/6] doesn't die on "Operation not permitted".
if [ -z "${ROOT_PREFIX}" ] && [ "$(id -u)" -ne 0 ]; then
    echo -e "${YELLOW}This recovery needs root - re-running with sudo...${NC}"
    exec sudo -E "$0" "$@"
fi

TARGET_USER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["user"])' "${STATE_DIR}/os_info.json" 2>/dev/null || echo "${SUDO_USER:-$USER}")"
TARGET_HOME="${ROOT_PREFIX}/home/${TARGET_USER}"

