# What did not get installed? (pacman prints only a warning for a failed batch)
if [ "${DO_PKGS}" = 1 ] && command -v pacman >/dev/null; then
    INSTALLED_NOW="$(run pacman -Qq </dev/null 2>/dev/null || true)"
    if [ -f "${STATE_DIR}/pkglist_explicit.txt" ]; then
        while IFS= read -r _p; do
            [ -n "${_p}" ] || continue
            grep -Fxq -- "${_p}" <<<"${INSTALLED_NOW}" || TODO+=("pacman -S ${_p}")
        done < "${STATE_DIR}/pkglist_explicit.txt"
    fi
    if [ -f "${STATE_DIR}/pkglist_aur.txt" ] && [ -s "${STATE_DIR}/pkglist_aur.txt" ]; then
        while IFS= read -r _p; do
            [ -n "${_p}" ] || continue
            grep -Fxq -- "${_p}" <<<"${INSTALLED_NOW}" || TODO+=("AUR: yay -S ${_p}")
        done < "${STATE_DIR}/pkglist_aur.txt"
    fi
fi

