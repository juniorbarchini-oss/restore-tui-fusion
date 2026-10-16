# Migrating to restore-fusion

> This page is only for people who used the earlier, separate `restore-tui` and `btrfs-restore-tui`
> editions. A fresh install needs nothing from it: see the README.

restore-fusion replaces **both** `restore-tui` (ext4, rsync + hardlinks, apt) and `btrfs-restore-tui` (btrfs
send/receive, pacman/AUR). One app, one set of commands: `restore-tui`, `restore-now`, `backup-now`.

The short version: **install it and keep going.** Your backups, your config and your local snapshots are not
touched and not converted. Nothing here needs a conversion step.

## What happens to what

| You have | After `sudo ./install.sh` |
|---|---|
| `/opt/restore-tui/` and `/opt/btrfs-restore-tui/` (the old apps) | **Moved** (not deleted) to `/opt/restore-fusion-archive/<name>-<UTC time>/`. Delete that folder yourself when you are happy. |
| Their desktop entries (`restore-tui.desktop`, `btrfs-restore.desktop`) | Moved into the same archive; a new `restore-fusion.desktop` is written. |
| `/usr/local/bin/{restore-tui,restore-now,backup-now}` | Re-pointed to `/opt/restore-fusion/bin/`. A command there that is **not** from one of the old apps is left alone with a warning. |
| `~/.local/bin/backup-now` (the loose script the btrfs edition retired) | Moved into the archive if it is not already ours. |
| `~/.config/restore-tui/config.conf` and/or `~/.config/btrfs-restore/config.conf` | **Unchanged.** Both locations are still read (first one found wins). If the file is readable by other users the installer asks whether to set mode 600 (default No). If you have no config, a fresh one is created with mode 600. |
| Backups on the USB stick: `restore-tui/` and `btrfs-restore/` folders | **Unchanged and still used.** New backups go to the folder of the engine that made them. `restore-now` lists both. |
| Backups on the SSH host (rsync tree `restore-tui/…`, btrfs tree `{root,home,meta}/…`) | **Unchanged.** The btrfs tree has no manifest and does not need one: completeness is decided by `received_uuid`, as before. |
| Local snapshots in `/.snapshots` | **Unchanged.** |

## From `btrfs-restore-tui` (Arch / Omarchy)

1. `sudo ./install.sh` from this repository.
2. `restore-tui --paths` shows every path the tool uses. They are the same locations as before plus
   `/opt/restore-fusion/`.
3. Your config keys all keep their names. New ones you may want: `ENGINE=auto` (default: btrfs on a btrfs
   system), `REMOTE_HOST_LAN`, `REMOTE_MAX_GB`. Nothing needs to be added for things to work.
4. Run `backup-now --dry-run` first. The first run after the switch **continues from your last completed
   backup** (incremental) when the parent snapshots are still there; it is a full backup if they are not, and
   it tells you before it starts.

Behaviour changes you will notice (all deliberate, see `README.md` "Safety rules"):
- No screen starts with anything ticked, and the restore confirmation has no default button (focus is on Cancel).
- Retention pruning is shown in `--dry-run` and goes through an explicit approval. `--yes` never approves a deletion.
- Restoring over an existing file keeps the old one as `.bak`, then `.bak.1`, `.bak.2`… An older `.bak` is never overwritten.
- A restore with failures ends as "restored with errors" and lists them.
- `PBS_PASSWORD` stays in `config.conf`, which is now seeded with mode 600.

## From `restore-tui` (Ubuntu, ext4)

1. `sudo ./install.sh` from this repository.
2. **No default host, IP or path ships any more.** The ext4 edition had a built-in default backup host and LAN
   address. If you relied on it, put your values in your own config (or use `[S]` Settings):
   ```
   REMOTE_HOST=<your host>
   REMOTE_HOST_LAN=<your LAN address, optional>
   REMOTE_PATH=<your path on the host>
   REMOTE_USER=<your user>
   ```
   Until you do, `backup-now` backs up to the USB only and says that no remote is configured.
3. The USB is never chosen as "the first drive". It must be a real mount point and either already hold a
   backup of this engine or carry the label in `TARGET_LABEL`. If your stick has neither, set `TARGET_DIR`.
4. `backup-now`'s destination menu no longer pre-ticks everything reachable: tick what you want.
5. rsync snapshots keep the same layout, so hardlinks to your previous snapshot continue to work
   (`--link-dest` baseline = the newest `completed` snapshot written by the rsync engine).
6. On a machine whose `/` is btrfs, `ENGINE=auto` now picks btrfs. To keep using rsync there, set
   `ENGINE=rsync` explicitly.

Behaviour changes: same list as above, plus:
- the ext4 PBS restore dialog (Backup / Overwrite / Cancel) is gone; PBS restore now uses the same screen
  and the same Backup (.bak) / Overwrite / Skip choices as `restore-now`, and shows all errors;
- PBS snapshot selection needs a number (Enter no longer means "newest");
- `rsync` exit 23 with no message ends as "restored with errors", never as success.

## Rolling back

Nothing is deleted, so rolling back is possible:

```bash
sudo /opt/restore-fusion/uninstall.sh --yes            # removes only restore-fusion
sudo mv /opt/restore-fusion-archive/restore-tui-<time> /opt/restore-tui
sudo mv /opt/restore-fusion-archive/btrfs-restore-tui-<time> /opt/btrfs-restore-tui
# re-create the old command links and rebuild each old venv with its own install.sh
```

Not verified: whether the old apps can read backups that restore-fusion wrote after the switch (the rsync
manifest is now version 2, with extra fields). Do not rely on rolling back *after* you have made new backups;
backups made *before* the switch are untouched and stay readable by the old apps.

## Check the migration worked

1. `restore-now` lists the snapshots from before the switch (tag `[USB]`, `[<remote name>]`).
2. `restore-tui --paths` matches what you expect.
3. `backup-now --dry-run` names the right drive and shows the right engine.
4. Run the relevant rows of [`HARDWARE_CHECKLIST.md`](HARDWARE_CHECKLIST.md) (H11 is the installer).
