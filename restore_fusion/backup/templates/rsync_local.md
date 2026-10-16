# Disaster recovery — @@HOST_NAME@@

This drive holds ext4/rsync backups of **@@HOST_NAME@@**, made by restore-fusion.
Newest snapshot: **@@LATEST@@**  (kit written @@WHEN@@).

## Recover a whole machine (bare metal)

1. Install a fresh Ubuntu (same release), log in as yourself.
2. Mount this drive.
3. From the folder that holds this file:

       ./disaster-recovery.sh

   It lists the snapshots, you pick one (default: the newest), and runs
   that snapshot's self-contained restore.sh: APT repos/keyrings, your
   manual packages (one at a time, so one unknown name never blocks the
   rest), Snaps (best-effort), Flatpaks, dconf, your /usr/local/bin
   scripts, crontab, and the home tree - reinstalling itself too if its
   own dev clone came back with $HOME. It ends with a plain "software to
   install" checklist for whatever it genuinely couldn't grab itself.

## Recover just a few files

Reinstall restore-fusion (source is in restore-fusion-src.tar.gz on this drive, or
from GitHub) and run `restore-now` for the retro file browser.

Needs only: bash, coreutils, rsync, apt. No Python, no this app - and no
network at all for the home tree (only for reinstalling packages).
