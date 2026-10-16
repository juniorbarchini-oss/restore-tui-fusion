# home_<ts> is either a received btrfs subvolume or a .btrfs.zst stream
HOME_SRC=""
for d in "${SCRIPT_DIR}"/home_*; do [ -d "$d" ] && HOME_SRC="$d"; done
HOME_STREAM=""; [ -f "${SCRIPT_DIR}/home.btrfs.zst" ] && HOME_STREAM="${SCRIPT_DIR}/home.btrfs.zst"

