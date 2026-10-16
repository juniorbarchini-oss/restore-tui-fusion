if [ -f "${STATE_DIR}/usr_local_bin_symlinks.txt" ]; then
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if [ "$SELF_REINSTALLED" = true ] && [[ "$line" == *"/opt/restore-tui/"* ]]; then
            continue  # install.sh above already put this one back
        fi
        TODO+=("reinstall: $line")
    done < "${STATE_DIR}/usr_local_bin_symlinks.txt"
fi

