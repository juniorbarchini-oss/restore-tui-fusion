# restore-tui-fusion

A terminal app (Python + [Textual](https://textual.textualize.io/)) to back up a Linux home directory and
system state, and to restore single files, folders or a whole machine from those backups.

> **Status: alpha.** The logic is covered by unit tests that use fakes. Everything that needs real disks,
> `sudo`, SSH, FUSE or a Proxmox server has **not** been fully verified on real hardware yet: see
> [`docs/HARDWARE_CHECKLIST.md`](docs/HARDWARE_CHECKLIST.md) for what was and was not tested.
> Do not make this your only backup.

> **Warning.** The installer and the backup/restore commands run with `sudo` and can **write to and
> overwrite real data** (restoring files over their original location, deleting old snapshots according to
> your retention settings). Read what a command will do first; `backup-now --dry-run` writes nothing.

## What it does

| Engine | Filesystem | How it copies | Package reinstall |
|---|---|---|---|
| `rsync` | ext4 and others (Ubuntu) | rsync + hardlinked snapshots | apt, snap, flatpak |
| `btrfs` | btrfs (Arch, Omarchy) | `btrfs send/receive` + zstd | pacman, AUR, flatpak |

- Backs up to a USB drive, a local folder and/or an SSH host (optionally to Proxmox Backup Server).
- The engine is picked automatically from the filesystem (`ENGINE=auto`), or forced in the config.
- Browse snapshots on a timeline, pick files or folders, restore them in place or to `~/recovered_…`.
- Generates a bare-metal recovery kit (`restore.sh`) that reinstalls packages and restores `$HOME`.
- Scope: Ubuntu and Arch/Omarchy. Other distributions are not supported.

## Requirements

- Linux with Python 3.10 or newer, `sudo`, `bash`, `openssh` and `rsync`.
- btrfs engine: `btrfs-progs` (and `zstd`, optionally `pv`).
- Optional: `sshfs` (browse a remote backup without downloading it; the installer installs it with your package manager when it can),
  `proxmox-backup-client` (PBS push and restore).
- Python packages (`textual`, `rich`) are installed into a private virtualenv by the installer.

## Install

```bash
git clone https://github.com/juniorbarchini-oss/restore-tui-fusion.git
cd restore-tui-fusion
sudo ./install.sh
```

The installer puts the app in `/opt/restore-fusion`, links `restore-tui`, `restore-now` and `backup-now`
into `/usr/local/bin`, and creates `~/.config/restore-tui/config.conf` (mode 600) **only if none exists**.
It never touches your backups or local snapshots.

**Updating.** The menu checks GitHub (`git ls-remote`, at most once a day, silent when offline) and shows
`[U] Update (new version ... available)` when `main` has moved. `[U]` asks for confirmation, clones `main`
to a temporary folder and runs its `install.sh --upgrade` (it asks for your sudo password in the terminal).
Turn the daily check off with `CHECK_UPDATES=false` in `config.conf`.

## Use

```bash
restore-tui              # menu: [B]ackup [R]estore [S]ettings [P]BS push [X]PBS restore [F]ull recovery [U]pdate
backup-now --dry-run     # show what a backup would do, write nothing
backup-now               # run a backup
restore-now              # browse snapshots and restore files
restore-tui --paths      # every path the tool touches
restore-tui --gc         # clear scratch / staging left by interrupted runs
```

Copy [`config.conf.example`](config.conf.example) to `~/.config/restore-tui/config.conf`, `chmod 600` it, and
fill in your own drive, SSH host and (optionally) PBS settings. No host, IP or path is shipped as a default.

To remove it: `sudo /opt/restore-fusion/uninstall.sh` (keeps your config, backups and snapshots unless you
explicitly choose otherwise).

## Safety rules

These are design rules the code and tests enforce:

- Never fall back to "first writable removable drive". The target must be a real mount point.
- Partial or failed runs never become the baseline and never move `latest`.
- Restore is honest: per-file errors accumulate and the final status says "restored with errors".
- Never auto-delete for the user. `--yes` does **not** approve deletions, and delete prompts default to No.
- The user controls the data: nothing is preselected.
- `$HOME` data gets a real guarantee; installed applications are best-effort plus an honest list of what
  was left out.

## More documentation

- [`docs/MIGRATION.md`](docs/MIGRATION.md): moving from the earlier separate editions (`restore-tui`, `btrfs-restore-tui`).
- [`docs/HARDWARE_CHECKLIST.md`](docs/HARDWARE_CHECKLIST.md): what still needs real-hardware verification.
- [`docs/PLAN_FUSION.md`](docs/PLAN_FUSION.md) and [`docs/DESIGN_DESTINATION_WIZARD.md`](docs/DESIGN_DESTINATION_WIZARD.md): design notes.
- Tests: `./run-tests.sh` (tests that need rsync, ssh, btrfs or root are reported as SKIPPED, never as passed).

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md).

## License

[MIT](LICENSE) © Humberto Barchini
