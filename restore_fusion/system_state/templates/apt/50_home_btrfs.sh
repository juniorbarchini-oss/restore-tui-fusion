# Home tree from a btrfs snapshot (btrfs engine on an apt system): home_<ts>/ is a received
# subvolume, home.btrfs.zst a compressed send stream. Needs btrfs-progs (+ zstd for streams).
HOME_SUBVOL=""
for _d in "${SCRIPT_DIR}"/home_*; do [ -d "${_d}" ] && HOME_SUBVOL="${_d}"; done
HOME_STREAM=""; [ -f "${SCRIPT_DIR}/home.btrfs.zst" ] && HOME_STREAM="${SCRIPT_DIR}/home.btrfs.zst"
if [ -n "${HOME_SUBVOL}" ] || [ -n "${HOME_STREAM}" ]; then
    echo -e "${BLUE}[4/5] Home files (~)...${NC}"
    if [ -n "${HOME_SUBVOL}" ]; then
        _src="${HOME_SUBVOL}"
        [ -d "${HOME_SUBVOL}/${USER:-$(id -un)}" ] && _src="${HOME_SUBVOL}/${USER:-$(id -un)}"
        rsync -aAX --numeric-ids --info=progress2 "${_src}/" "${HOME}/"
    else
        STAGE="$(mktemp -d)"; echo "  receiving stream into ${STAGE}"
        if zstd -dc "${HOME_STREAM}" | sudo btrfs receive "${STAGE}"; then
            SUB="$(find "${STAGE}" -maxdepth 1 -mindepth 1 -type d | head -1)"
            _src="${SUB}"
            [ -d "${SUB}/${USER:-$(id -un)}" ] && _src="${SUB}/${USER:-$(id -un)}"
            rsync -aAX --numeric-ids --info=progress2 "${_src}/" "${HOME}/"
            sudo btrfs subvolume delete "${SUB}" 2>/dev/null || true
        else
            TODO+=("restore your home yourself: zstd -dc ${HOME_STREAM} | sudo btrfs receive <dir>")
        fi
        rm -rf "${STAGE}"
    fi
fi

