if [ -d "${HOME_SOURCE}" ]; then
    echo -e "${BLUE}[4/5] Home files (~)...${NC}"
    rsync -aAX --numeric-ids --info=progress2 "${HOME_SOURCE}/" "${HOME}/"
fi

