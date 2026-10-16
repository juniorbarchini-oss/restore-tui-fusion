# [+] Re-install this app itself (and it alone - see the /usr/local/bin
# symlinks list above for other custom /opt-based tools) properly, via its
# own install.sh, if your dev clone came back with $HOME. Deliberately NOT
# "run it straight out of ~/Documents/desarrollo" - that was an older
# project's habit and it means a half-installed app with no real /opt/
# deployment or /usr/local/bin symlinks; this puts it back exactly where
# install.sh always puts it.
SELF_REINSTALLED=false
for _clone in "${HOME}/Documents/desarrollo/restore-tui" "${HOME}/Documents/desarrollo/alienback"; do
    if [ -f "${_clone}/restore_tui/cli.py" ] && [ -x "${_clone}/install.sh" ]; then
        echo -e "${BLUE}[+] Reinstalling restore-tui from ${_clone}...${NC}"
        if sudo "${_clone}/install.sh"; then
            SELF_REINSTALLED=true
        else
            TODO+=("re-run sudo ${_clone}/install.sh yourself")
        fi
        break
    fi
done

