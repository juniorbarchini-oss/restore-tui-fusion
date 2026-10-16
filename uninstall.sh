#!/usr/bin/env bash
# ==============================================================================
# restore-fusion uninstaller
#
#   sudo ./uninstall.sh            interactive
#   sudo ./uninstall.sh --yes      remove the app only; keep config, snapshots and the archive, no prompts
#   sudo ./uninstall.sh --purge    ALSO ask about the user config (still asks; typed confirmation)
#
# Removes exactly the paths install.sh recorded in /opt/restore-fusion/.owned-paths (the app dir, the
# command symlinks that point into it, the desktop entry). It never touches:
#   * your backups on USB / the SSH host;
#   * /opt/restore-fusion-archive (the retired old installs: delete that folder yourself);
#   * ~/.config/restore-tui/config.conf and ~/.config/btrfs-restore/config.conf (the config is shared
#     with the old editions) unless you type the confirmation;
#   * local snapshots in /.snapshots unless you type DELETE on a terminal (--yes can never do this).
#
# At the end it searches EVERY mounted filesystem that matters (not just the one holding /) for leftovers,
# and prints them split into "expected" (what you chose to keep) and "unexpected".
#
# Test switches: DESTDIR, RF_USER, RF_HOME as in install.sh; RF_MOUNTS="/ /home" overrides the mount list.
# ==============================================================================
set -euo pipefail

APP="restore-fusion"
DESTDIR="${DESTDIR:-}"
DESTDIR="${DESTDIR%/}"
OPT_DIR="/opt/${APP}"
ARCHIVE_DIR="/opt/${APP}-archive"
SNAP_DIR="${RF_SNAP_DIR:-/.snapshots}"

ASSUME_YES=0; PURGE=0
for a in "$@"; do
    case "$a" in
        --yes|-y) ASSUME_YES=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,/^# =====/p' "$0" | sed 's/^# \{0,1\}//' | head -n -1; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

if [ -z "${DESTDIR}" ] && [ "$(id -u)" -ne 0 ]; then
    echo "[!] needs root - re-running with sudo"
    exec sudo "$0" "$@"
fi

# This script lives inside the directory it deletes: run from a temporary copy.
if [ -z "${RF_UNINSTALL_COPY:-}" ]; then
    _tmp="$(mktemp)"; cp -- "$0" "${_tmp}"
    RF_UNINSTALL_COPY="${_tmp}" exec bash "${_tmp}" "$@"
fi
trap 'rm -f -- "${RF_UNINSTALL_COPY}"' EXIT

REAL_USER="${RF_USER:-${SUDO_USER:-$(id -un)}}"
REAL_HOME="${RF_HOME:-$(getent passwd "${REAL_USER}" | cut -d: -f6)}"
D() { printf '%s%s' "${DESTDIR}" "$1"; }

_ask() {  # explicit yes only; --yes, no tty and empty answer are all No
    [ "${ASSUME_YES}" -eq 1 ] && return 1
    [ -t 0 ] || return 1
    local ans; read -r -p "$1 [y/N] " ans || return 1
    [[ "${ans}" =~ ^[Yy]$ ]]
}

echo "=== Uninstalling ${APP} ==="

# 1. leftovers of dead runs, before the code goes away (best effort)
if [ -z "${DESTDIR}" ] && [ -x "${OPT_DIR}/bin/restore-tui" ]; then
    "${OPT_DIR}/bin/restore-tui" --gc || true
fi

# 2. the app: only what install.sh recorded
OWNED_FILE="$(D "${OPT_DIR}")/.owned-paths"
KEPT=()
if [ -f "${OWNED_FILE}" ]; then
    while IFS= read -r p; do
        [ -n "${p}" ] || continue
        case "${p}" in
            "${OPT_DIR}") continue ;;   # removed last, below
            /usr/local/bin/*)
                t="$(D "${p}")"
                if [ -L "${t}" ] && [[ "$(readlink "${t}")" == "${OPT_DIR}"/* ]]; then
                    rm -f -- "${t}"; echo "--> removed ${p}"
                elif [ -e "${t}" ] || [ -L "${t}" ]; then
                    KEPT+=("${p} (not ours any more - left)")
                fi ;;
            /usr/share/applications/*)
                if [ -f "$(D "${p}")" ]; then rm -f -- "$(D "${p}")"; echo "--> removed ${p}"; fi ;;
            *) echo "[!] ignoring unexpected entry in .owned-paths: ${p}" >&2 ;;
        esac
    done < "${OWNED_FILE}"
    if [ -d "$(D "${OPT_DIR}")" ] && [ -f "$(D "${OPT_DIR}")/restore_fusion/cli.py" ]; then
        rm -rf -- "$(D "${OPT_DIR}")"; echo "--> removed ${OPT_DIR}"
    else
        echo "[!] ${OPT_DIR} does not look like our install - left in place" >&2
        KEPT+=("${OPT_DIR} (unrecognised)")
    fi
else
    echo "[!] ${OWNED_FILE} not found: nothing recorded by install.sh, so nothing was removed." >&2
    KEPT+=("${OPT_DIR} (no install record)")
fi

# 3. user config - shared with the old editions, so it needs an explicit yes
CFG_DIRS=("$(D "${REAL_HOME}")/.config/restore-tui" "$(D "${REAL_HOME}")/.config/btrfs-restore")
if [ "${PURGE}" -eq 1 ] && _ask "Also remove the user config in ${REAL_HOME}/.config/{restore-tui,btrfs-restore}/ ?"; then
    for d in "${CFG_DIRS[@]}"; do
        [ -f "${d}/config.conf" ] && rm -f -- "${d}/config.conf" && echo "--> removed ${d#"${DESTDIR}"}/config.conf"
        rmdir "${d}" 2>/dev/null || true
    done
else
    for d in "${CFG_DIRS[@]}"; do [ -d "${d}" ] && KEPT+=("${d#"${DESTDIR}"}/ (your config)"); done
fi

# 4. local snapshots: never with --yes, never without a terminal, only by typing DELETE
SNAPS=()
if [ -d "$(D "${SNAP_DIR}")" ]; then
    for s in "$(D "${SNAP_DIR}")"/root_* "$(D "${SNAP_DIR}")"/home_*; do [ -e "${s}" ] && SNAPS+=("${s}"); done
fi
if [ "${#SNAPS[@]}" -gt 0 ]; then
    echo; echo "  Local snapshots in ${SNAP_DIR}:"; printf '    %s\n' "${SNAPS[@]#"${DESTDIR}"}"
    echo "  These may be your only local copies. Backups on USB / the SSH host are NOT touched."
    if [ "${ASSUME_YES}" -eq 0 ] && [ -t 0 ]; then
        read -r -p "  Type DELETE to remove them, anything else to keep: " typed || typed=""
        if [ "${typed}" = "DELETE" ]; then
            for s in "${SNAPS[@]}"; do
                btrfs subvolume delete "${s}" 2>/dev/null || echo "[!] could not delete ${s#"${DESTDIR}"} (btrfs subvolume delete failed) - kept"
            done
        fi
    fi
    for s in "${SNAPS[@]}"; do [ -e "${s}" ] && KEPT+=("${s#"${DESTDIR}"} (local snapshot)"); done
fi

# 5. the archive of retired installs is never removed here
[ -d "$(D "${ARCHIVE_DIR}")" ] && KEPT+=("${ARCHIVE_DIR}/ (retired old installs - delete it yourself when you are sure)")

echo
echo "=== Done ==="
if [ "${#KEPT[@]}" -gt 0 ]; then
    echo "Kept on purpose:"; printf '  %s\n' "${KEPT[@]}"
fi

# 6. residue check across every mounted filesystem that matters (R12: `find / -xdev` stops at /home
#    when /home is its own mount, so each mount point is searched separately).
if [ -n "${RF_MOUNTS:-}" ]; then
    read -r -a MOUNTS <<< "${RF_MOUNTS}"
elif command -v findmnt >/dev/null 2>&1; then
    mapfile -t MOUNTS < <(findmnt -rn -o TARGET,FSTYPE | awk '$2 ~ /^(ext[234]|btrfs|xfs|f2fs|zfs)$/ {print $1}' \
        | grep -Ev '^(/media|/run|/mnt|/sys|/proc|/dev|/\.snapshots|/var/lib/docker|/var/lib/containers)' || true)
else
    MOUNTS=(/)
fi
FOUND=()
for m in "${MOUNTS[@]}"; do
    root="$(D "${m}")"
    [ -d "${root}" ] || continue
    while IFS= read -r hit; do FOUND+=("${hit#"${DESTDIR}"}"); done < <(
        find "${root}" -xdev \( -path "${root%/}/.snapshots" -o -path "${root%/}/proc" -o -path "${root%/}/sys" \) -prune -o \
            \( -name 'restore-fusion*' -o -name '*btrfs-restore*' -o -path '*/bin/restore-tui' -o -path '*/bin/restore-now' \
               -o -path '*/bin/backup-now' -o -name 'restore-tui.desktop' \) -print 2>/dev/null || true)
done
echo
echo "Searched these filesystems for leftovers: ${MOUNTS[*]}"
if [ "${#FOUND[@]}" -eq 0 ]; then
    echo "Nothing left behind."
else
    UNEXPECTED=()
    for f in "${FOUND[@]}"; do
        case "${f}" in
            "${ARCHIVE_DIR}"|"${ARCHIVE_DIR}"/*|*/.config/restore-tui*|*/.config/btrfs-restore*) ;;  # expected: kept on purpose
            *) UNEXPECTED+=("${f}") ;;
        esac
    done
    if [ "${#UNEXPECTED[@]}" -eq 0 ]; then
        echo "Only what you chose to keep is left (listed above)."
    else
        echo "Unexpected leftovers (review and remove by hand if they are yours):"
        printf '  %s\n' "${UNEXPECTED[@]}"
    fi
fi
