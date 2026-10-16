if [ "${DO_HOME}" = 1 ]; then
    echo -e "${BLUE}[5/6] home tree -> ${TARGET_HOME}${NC}"
    mkdir -p "${TARGET_HOME}"
    if [ -n "${HOME_SRC}" ] && [ -d "${HOME_SRC}/${TARGET_USER}" ]; then
        rsync -aAX --numeric-ids --info=progress2 "${HOME_SRC}/${TARGET_USER}/" "${TARGET_HOME}/"
    elif [ -n "${HOME_SRC}" ]; then
        rsync -aAX --numeric-ids --info=progress2 "${HOME_SRC}/" "${TARGET_HOME}/"
    elif [ -n "${HOME_STREAM}" ]; then
        STAGE="$(mktemp -d)"; echo "  receiving stream into ${STAGE}"
        zstd -dc "${HOME_STREAM}" | sudo btrfs receive "${STAGE}"
        SUB="$(find "${STAGE}" -maxdepth 1 -mindepth 1 -type d | head -1)"
        rsync -aAX --numeric-ids --info=progress2 "${SUB}/${TARGET_USER:-}/" "${TARGET_HOME}/" 2>/dev/null \
            || rsync -aAX --numeric-ids --info=progress2 "${SUB}/" "${TARGET_HOME}/"
        sudo btrfs subvolume delete "${SUB}" 2>/dev/null || true; rm -rf "${STAGE}"
    else
        echo -e "${YELLOW}  no home data in this snapshot${NC}"
    fi
    if id "${TARGET_USER}" >/dev/null 2>&1; then
        chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}" || true
    fi
fi

