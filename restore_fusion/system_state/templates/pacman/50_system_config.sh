echo -e "${BLUE}[6/6] system config${NC}"
if [ "${DO_ETC}" = 1 ]; then
    [ -d "${STATE_DIR}/etc" ] && run cp -n "${STATE_DIR}/etc/." "${ROOT_PREFIX}/etc/" 2>/dev/null || true
    [ -d "${STATE_DIR}/keyd" ] && { run mkdir -p /etc/keyd; run cp -n "${STATE_DIR}/keyd/." /etc/keyd/ 2>/dev/null || true; }
    if [ -d "${STATE_DIR}/usr_local_bin" ]; then
        run mkdir -p /usr/local/bin
        run cp -n "${STATE_DIR}/usr_local_bin/." /usr/local/bin/ 2>/dev/null || true
    fi
    if [ -f "${STATE_DIR}/systemd_enabled.txt" ]; then
        awk '{print $1}' "${STATE_DIR}/systemd_enabled.txt" | while read -r u; do
            [ -n "$u" ] && run systemctl enable "$u" 2>/dev/null || true
        done
    fi
    [ -f "${STATE_DIR}/crontab.txt" ] && sudo -u "${TARGET_USER}" crontab "${STATE_DIR}/crontab.txt" || true
fi

