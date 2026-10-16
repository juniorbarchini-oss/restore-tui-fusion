if [ -f "${STATE_DIR}/crontab.txt" ]; then
    echo -e "${BLUE}[+] User crontab...${NC}"
    crontab "${STATE_DIR}/crontab.txt" || true
fi

