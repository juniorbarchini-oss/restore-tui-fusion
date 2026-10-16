if [ "${DO_HOME}" = 1 ]; then
    echo -e "${BLUE}[5/6] home tree -> ${TARGET_HOME}${NC}"
    mkdir -p "${TARGET_HOME}"
    if [ -n "${HOME_SRC}" ]; then
        rsync -aAX --numeric-ids --info=progress2 "${HOME_SRC}/" "${TARGET_HOME}/"
    else
        echo -e "${YELLOW}  no home data in this snapshot${NC}"
    fi
    if id "${TARGET_USER}" >/dev/null 2>&1; then
        chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}" || true
    fi
fi

