#!/usr/bin/env bash
# =============================================================================
# btrfs-restore-tui - Disaster Recovery entry point
# You are reading this from the root of a backup folder. It lists the snapshots
# here and runs the one you pick (default: newest) via its own restore.sh.
# Args after -- / unknown are passed through to restore.sh (e.g. --root /mnt).
# Needs: bash, coreutils, btrfs-progs, rsync, zstd, pacman. No Python, no network.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAPS="${HERE}/snapshots"
[ -d "${SNAPS}" ] || { echo "no snapshots/ directory next to this script"; exit 1; }

mapfile -t ALL < <(find "${SNAPS}" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | sort -r)
[ "${#ALL[@]}" -gt 0 ] || { echo "no snapshots found in ${SNAPS}"; exit 1; }

status_of() { grep -o '"status": *"[a-z]*"' "${SNAPS}/$1/manifest.json" 2>/dev/null \
                | grep -o '[a-z]*"$' | tr -d '"' || echo "unknown"; }
size_of()   { du -sh "${SNAPS}/$1" 2>/dev/null | cut -f1 || echo "?"; }

echo "Snapshots on this drive (newest first):"
i=0
for s in "${ALL[@]}"; do
    printf "  [%d] %-19s  %-9s  %s\n" "${i}" "${s}" "$(status_of "${s}")" "$(size_of "${s}")"
    i=$((i + 1))
done

DEFAULT="$(basename "$(readlink "${HERE}/latest" 2>/dev/null || true)" 2>/dev/null || true)"
[ -n "${DEFAULT}" ] || DEFAULT="${ALL[0]}"
read -r -p "Snapshot to restore (name or number) [${DEFAULT}]: " PICK
PICK="${PICK:-${DEFAULT}}"
case "${PICK}" in ''|*[!0-9]*) ;; *) PICK="${ALL[${PICK}]:-${PICK}}" ;; esac

SNAP="${SNAPS}/${PICK}"
[ -d "${SNAP}" ]        || { echo "no such snapshot: ${PICK}"; exit 1; }
[ -x "${SNAP}/restore.sh" ] || { echo "${PICK} has no runnable restore.sh"; exit 1; }

echo
echo "=== ${PICK} ==="
if [ -f "${SNAP}/manifest.json" ]; then
    grep -E '"(status|created_at|hostname)"' "${SNAP}/manifest.json" | sed 's/^ */  /'
fi
[ -f "${SNAP}/_system_state/pkglist_explicit.txt" ] && \
    echo "  packages : $(wc -l < "${SNAP}/_system_state/pkglist_explicit.txt")"
for _h in "${SNAP}"/home_*; do
    [ -d "${_h}" ] && echo "  home     : $(du -sh "${_h}" 2>/dev/null | cut -f1)"
done
echo
read -r -p "Run ${PICK}/restore.sh now? type 'yes': " C
[ "${C}" = "yes" ] || { echo "aborted"; exit 0; }

cd "${SNAP}"
exec ./restore.sh "$@"
