if [ -d "${STATE_DIR}/apt_sources" ]; then
    echo -e "${BLUE}[1/5] APT repositories & keyrings...${NC}"
    [ -d "${STATE_DIR}/apt_sources/keyrings" ] && sudo cp -n "${STATE_DIR}/apt_sources/keyrings/"* /etc/apt/keyrings/ 2>/dev/null || true
    [ -d "${STATE_DIR}/apt_sources/trusted.gpg.d" ] && sudo cp -n "${STATE_DIR}/apt_sources/trusted.gpg.d/"* /etc/apt/trusted.gpg.d/ 2>/dev/null || true
    [ -d "${STATE_DIR}/apt_sources/sources.list.d" ] && sudo cp -n "${STATE_DIR}/apt_sources/sources.list.d/"* /etc/apt/sources.list.d/ 2>/dev/null || true
    [ -d "${STATE_DIR}/apt_sources/usr_share_keyrings" ] && sudo mkdir -p /usr/share/keyrings && sudo cp -n "${STATE_DIR}/apt_sources/usr_share_keyrings/"* /usr/share/keyrings/ 2>/dev/null || true
    sudo apt-get update -y || true
fi

