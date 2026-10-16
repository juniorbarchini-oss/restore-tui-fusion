if [ -d "${STATE_DIR}/usr_local_bin" ]; then
    echo -e "${BLUE}[+] Custom /usr/local/bin scripts...${NC}"
    sudo cp -n "${STATE_DIR}/usr_local_bin/"* /usr/local/bin/ 2>/dev/null || true
fi

