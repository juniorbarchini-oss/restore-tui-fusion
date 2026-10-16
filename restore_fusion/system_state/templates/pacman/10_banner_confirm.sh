echo -e "${GREEN}=================================================================${NC}"
echo -e "${GREEN}   btrfs-restore-tui - BARE-METAL RECOVERY (Arch / Omarchy)       ${NC}"
echo -e "${GREEN}=================================================================${NC}"
echo    "Snapshot : ${SCRIPT_DIR}"
echo    "User     : ${TARGET_USER}"
echo    "Home     : ${TARGET_HOME}"
[ -n "${ROOT_PREFIX}" ] && echo "Root pfx : ${ROOT_PREFIX}"
echo ""
read -r -p "Proceed with recovery from this snapshot? (yes/no): " C
[ "${C}" = "yes" ] || { echo -e "${YELLOW}Aborted.${NC}"; exit 0; }

run() { if [ -n "${ROOT_PREFIX}" ]; then arch-chroot "${ROOT_PREFIX}" "$@"; else sudo "$@"; fi; }

