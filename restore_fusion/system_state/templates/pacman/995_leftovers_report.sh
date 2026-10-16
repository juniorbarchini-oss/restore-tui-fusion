
if [ "${#TODO[@]}" -gt 0 ]; then
    echo ""
    echo -e "${YELLOW}=================================================================${NC}"
    echo -e "${YELLOW}  SOFTWARE TO INSTALL (grab these yourself, like an App Store    ${NC}"
    echo -e "${YELLOW}  shopping list - not an error):                                 ${NC}"
    echo -e "${YELLOW}=================================================================${NC}"
    printf '  - %s\n' "${TODO[@]}"
fi
