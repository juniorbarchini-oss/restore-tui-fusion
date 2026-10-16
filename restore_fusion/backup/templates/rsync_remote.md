# Disaster recovery from @@REMOTE@@ — @@HOST_NAME@@

This folder is at the root of **@@HOST_NAME@@**'s backups on the SSH host
`@@REMOTE@@` (`@@BASE@@`), made by restore-fusion. Kit written @@WHEN@@.

## Recover a machine (bare metal)

1. Install a fresh Ubuntu (same release) on the new machine, log in as
   yourself.
2. Make sure you can `ssh @@REMOTE@@` (copy a key, or answer the password
   prompt when disaster-recovery.sh runs it).
3. Copy `disaster-recovery.sh` from here to the new machine and run it there:

       ./disaster-recovery.sh                 # newest snapshot
       ./disaster-recovery.sh --snapshot 3    # pick from the list

   It pulls that snapshot down to `/var/tmp/restore-tui-recovery/<name>/`
   (override with `RESTORE_TUI_WORK=`) and runs its own restore.sh: APT
   repos/keyrings, manual packages, Snaps, Flatpaks, dconf, your
   /usr/local/bin scripts, crontab, the home tree, and a "software to
   install" checklist for whatever it couldn't grab itself.

## Notes

- Needs only: bash, coreutils, rsync, openssh, apt. No Python, no this app.
- The app source is in restore-fusion-src.tar.gz here (for `restore-now`, the
  retro file browser, once the machine is back up).
