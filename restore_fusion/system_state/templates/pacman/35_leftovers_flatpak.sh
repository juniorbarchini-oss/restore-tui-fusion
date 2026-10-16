if [ -f "${STATE_DIR}/flatpak_list.txt" ] && command -v flatpak >/dev/null; then
    while IFS= read -r _a; do
        [ -n "${_a}" ] || continue
        flatpak info "${_a}" >/dev/null 2>&1 || TODO+=("flatpak install flathub ${_a}")
    done < "${STATE_DIR}/flatpak_list.txt"
fi

