# Disaster recovery from @@REMOTE@@ — @@HOST_NAME@@

This folder is at the root of **@@HOST_NAME@@**'s Btrfs backups on the SSH host
`@@REMOTE@@` (`@@BASE@@`), made by restore-fusion. Kit written @@WHEN@@.

The remote layout keeps each backup in three places:

    @@BASE@@/root/root_<compact>     received / subvolume
    @@BASE@@/home/home_<compact>     received /home subvolume
    @@BASE@@/meta/<name>/            _system_state/ + restore.sh

(`<name>` is `YYYY-MM-DD_HHMMSS`; `<compact>` is the same without the dashes.)

## Recover a machine (bare metal)

1. Install a fresh Arch base on the new machine (btrfs root, your user created),
   or boot a live ISO with the new root mounted at /mnt.
2. Make sure you can `ssh @@REMOTE@@` (copy a key or use a password).
3. Copy `disaster-recovery.sh` from here to the new machine and run it there:

       ./disaster-recovery.sh                 # newest snapshot
       ./disaster-recovery.sh --snapshot 3    # pick from the list
       ./disaster-recovery.sh -- --root /mnt  # from a live ISO

   It pulls that snapshot's `_system_state/` + home subvolume into
   `/var/tmp/btrfs-restore-recovery/<name>/` (override with `BTRFS_RESTORE_WORK=`)
   and runs its `restore.sh` (pacman config + mirrors, explicit + AUR packages,
   Flatpaks, /etc bits, systemd units, home).

4. Afterwards: check /etc/fstab and the bootloader config under
   `_system_state/bootloader/`, run `sudo mkinitcpio -P` if needed, reboot.

## Restore the exact root filesystem instead of replaying packages

With the new root mounted at /mnt:

    ssh @@REMOTE@@ 'sudo btrfs send @@BASE@@/root/root_<compact>' \
        | sudo btrfs receive /mnt
    # then fix /mnt/etc/fstab + the bootloader for the new disk UUIDs.

## Notes

- Needs only: bash, coreutils, openssh, rsync, btrfs-progs, zstd. No Python, no app.
- If python3 is missing on the fresh machine, `export SUDO_USER=<you>` before
  running so restore.sh targets the right home.
- The app source is in `restore-fusion-src.tar.gz` here (for the retro file
  browser once the machine is back up).
