#!/usr/bin/env bash
# =============================================================================
# btrfs-restore-tui - Disaster Recovery from the SSH backup host
# Run this ON THE MACHINE BEING RECOVERED (fresh Arch base, or live ISO with the
# new root at /mnt). It needs SSH access to the backup host. It pulls one
# snapshot's system state + home subvolume down and hands off to its restore.sh.
# Args after `--` are passed through to restore.sh (e.g. --root /mnt).
# Needs: bash, coreutils, openssh, rsync, btrfs-progs, zstd. No Python, no app.
# =============================================================================
set -euo pipefail

HOST="@@HOST@@"
BASE="@@BASE@@"
PORT="@@PORT@@"
# staging lives outside any /home so restore.sh's final `chown -R /home/<user>`
# never recurses into the read-only received subvolume we drop here.
WORK="${BTRFS_RESTORE_WORK:-/var/tmp/btrfs-restore-recovery}"
PICK=""

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
[ "${PORT}" != "22" ] && SSH_OPTS+=(-p "${PORT}")

PASS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --host)     HOST="$2"; shift 2 ;;
        --snapshot) PICK="$2"; shift 2 ;;
        --)         shift; PASS=("$@"); break ;;
        -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)          echo "unknown option: $1"; exit 2 ;;
    esac
done

echo "Backup host : ${HOST}:${BASE}"
# -n on every standalone ssh: without it ssh swallows this script's stdin and
# the `read` prompts below get EOF. (rsync manages its own ssh, leave that one.)
mapfile -t NAMES < <(ssh -n "${SSH_OPTS[@]}" "${HOST}" \
    "find ${BASE}/meta -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null" \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}$' | sort -r)
[ "${#NAMES[@]}" -gt 0 ] || { echo "no snapshots under ${HOST}:${BASE}/meta"; exit 1; }

echo "Snapshots on the host (newest first):"
i=0
for n in "${NAMES[@]}"; do printf '  [%d] %s\n' "${i}" "${n}"; i=$((i + 1)); done

DEFAULT="${NAMES[0]}"
if [ -z "${PICK}" ]; then
    read -r -p "Snapshot to restore (name or number) [${DEFAULT}]: " PICK
    PICK="${PICK:-${DEFAULT}}"
fi
case "${PICK}" in ''|*[!0-9]*) ;; *) PICK="${NAMES[${PICK}]:-${PICK}}" ;; esac
COMPACT="${PICK//-/}"
echo
echo "=== ${PICK}  (subvolumes home_${COMPACT} / root_${COMPACT}) ==="

SNAP="${WORK}/${PICK}"
mkdir -p "${SNAP}"

echo "[1/3] system state + restore.sh  ->  ${SNAP}"
rsync -aAX -e "ssh ${SSH_OPTS[*]}" "${HOST}:${BASE}/meta/${PICK}/" "${SNAP}/"

echo "[2/3] home subvolume"
if ssh -n "${SSH_OPTS[@]}" "${HOST}" "sudo -n btrfs subvolume show ${BASE}/home/home_${COMPACT}" >/dev/null 2>&1; then
    ssh -n "${SSH_OPTS[@]}" "${HOST}" "sudo btrfs send ${BASE}/home/home_${COMPACT}" \
        | sudo btrfs receive "${SNAP}/"
else
    echo "  ! cannot read ${BASE}/home/home_${COMPACT} on ${HOST}"
    echo "    (need working 'sudo btrfs' there) - restore.sh will run without home data"
fi

echo "[3/3] hand off to restore.sh"
[ -x "${SNAP}/restore.sh" ] || { echo "no runnable restore.sh in ${SNAP}"; exit 1; }
if [ -f "${SNAP}/_system_state/pkglist_explicit.txt" ]; then
    echo "  packages : $(wc -l < "${SNAP}/_system_state/pkglist_explicit.txt")"
fi
echo
read -r -p "Run ${PICK}/restore.sh now? type 'yes': " C
[ "${C}" = "yes" ] || { echo "aborted (pulled data kept in ${SNAP})"; exit 0; }

cd "${SNAP}"
exec ./restore.sh "${PASS[@]}"
