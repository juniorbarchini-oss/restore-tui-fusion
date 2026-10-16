# Disaster recovery — @@HOST_NAME@@

This drive holds Btrfs backups of **@@HOST_NAME@@**, made by restore-fusion.
Newest snapshot: **@@LATEST@@**  (kit written @@WHEN@@).

## Recover a whole machine (bare metal)

1. Boot the new machine from an Arch install USB / live ISO, or a fresh Arch base.
2. Mount this drive. If you are on a live ISO, also mount the new root at /mnt.
3. From the folder that holds this file:

       ./disaster-recovery.sh              # from a booted system
       ./disaster-recovery.sh --root /mnt  # from a live ISO (new root at /mnt)

   It lists the snapshots, you pick one (default: the newest), and it runs that
   snapshot's self-contained restore.sh: pacman config + mirrors, explicit and
   AUR packages, Flatpaks, /etc bits, systemd units, and the home tree.

4. Afterwards: review /etc/fstab and the bootloader config under the snapshot's
   _system_state/bootloader/, run `sudo mkinitcpio -P` if needed, reboot.

## Recover just a few files

Reinstall the app (source is in restore-fusion-src.tar.gz on this drive, or
from GitHub) and run `restore-now` for the retro file browser.

Needs only: bash, coreutils, btrfs-progs, rsync, zstd, pacman. No Python, no
network.
