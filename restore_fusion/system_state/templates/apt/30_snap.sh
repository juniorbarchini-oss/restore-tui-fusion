if [ -f "${STATE_DIR}/snap_list.txt" ]; then
    # Best-effort auto-install, one at a time (--classic is a no-op error for
    # a strict-confinement snap, just a failed attempt, not a broken system -
    # LinuxBackup's own recovery does the same and it reliably brings back
    # most-to-all real apps). Skip OS runtime/base snaps - they reappear on
    # their own as dependencies and installing them directly is meaningless.
    while IFS= read -r line; do
        name="$(awk '{print $1}' <<<"$line")"
        case "$name" in
            ""|Name|bare|core|core1*|core2*|core3*|snapd|snapd-desktop-integration| \
            gtk-common-themes|desktop-security-center|firmware-updater| \
            prompting-client|snap-store|gnome-*|mesa-*|gaming-graphics-*)
                continue ;;
        esac
        sudo snap install "$name" --classic >/dev/null 2>&1 || TODO+=("snap install $name")
    done < "${STATE_DIR}/snap_list.txt"
fi

