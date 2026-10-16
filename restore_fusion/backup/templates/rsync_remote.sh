#!/usr/bin/env bash
# =============================================================================
# restore-tui - Disaster Recovery from the SSH backup host
# Run this ON THE MACHINE BEING RECOVERED (fresh Ubuntu, logged in as
# yourself). It needs SSH access to the backup host. It pulls one snapshot
# down and hands off to its own restore.sh.
# Needs: bash, coreutils, rsync, openssh, apt. No Python, no this app.
# =============================================================================
set -euo pipefail

HOST="@@HOST@@"
BASE="@@BASE@@"
PORT="@@PORT@@"
WORK="${RESTORE_TUI_WORK:-/var/tmp/restore-tui-recovery}"
PICK=""

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
[ "${PORT}" != "22" ] && SSH_OPTS+=(-p "${PORT}")

while [ $# -gt 0 ]; do
    case "$1" in
        --host)     HOST="$2"; shift 2 ;;
        --snapshot) PICK="$2"; shift 2 ;;
        -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)          echo "unknown option: $1"; exit 2 ;;
    esac
done

echo "Backup host : ${HOST}:${BASE}"
# -n: without it ssh swallows this script's stdin and the `read` prompts
# below get EOF. rsync manages its own ssh transport, leave that one alone.
mapfile -t NAMES < <(ssh -n "${SSH_OPTS[@]}" "${HOST}" \
    "find ${BASE}/snapshots -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null" \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}(_[0-9])?$' | sort -r)
[ "${#NAMES[@]}" -gt 0 ] || { echo "no snapshots under ${HOST}:${BASE}/snapshots"; exit 1; }

echo "Snapshots on the host (newest first):"
i=0
for n in "${NAMES[@]}"; do printf '  [%d] %s\n' "${i}" "${n}"; i=$((i + 1)); done

DEFAULT="${NAMES[0]}"
if [ -z "${PICK}" ]; then
    read -r -p "Snapshot to restore (name or number) [${DEFAULT}]: " PICK
    PICK="${PICK:-${DEFAULT}}"
fi
case "${PICK}" in ''|*[!0-9]*) ;; *) PICK="${NAMES[${PICK}]:-${PICK}}" ;; esac

mkdir -p "${WORK}/${PICK}"
echo "Pulling ${HOST}:${BASE}/snapshots/${PICK}/ -> ${WORK}/${PICK}/ ..."
rsync -a --numeric-ids --info=progress2 -e "ssh ${SSH_OPTS[*]}" \
    "${HOST}:${BASE}/snapshots/${PICK}/" "${WORK}/${PICK}/"

SNAP="${WORK}/${PICK}"
[ -x "${SNAP}/restore.sh" ] || { echo "${PICK} has no runnable restore.sh"; exit 1; }

echo
echo "=== ${PICK} ==="
[ -f "${SNAP}/manifest.json" ] && \
    grep -E '"(status|created_at|hostname)"' "${SNAP}/manifest.json" | sed 's/^ */  /'
echo
read -r -p "Run ${PICK}/restore.sh now? type 'yes': " C
[ "${C}" = "yes" ] || { echo "aborted"; exit 0; }

cd "${SNAP}"
exec ./restore.sh
