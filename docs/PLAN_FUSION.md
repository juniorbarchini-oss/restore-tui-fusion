# PLAN: fuse `restore-tui` (ext4/rsync) and `btrfs-restore-tui` into one app

Status: historical design document (written as a plan, before the code existed). It is kept for the
reasoning behind the architecture; the implemented code in `restore_fusion/` is the source of truth.
It was written against snapshots of two earlier projects that are not part of this repository.

How to read the citations: `B:` = the btrfs edition's package (`btrfs_restore/`),
`E:` = the ext4 edition's package (`restore_tui/`), `Bt:` / `Et:` = the `tests/` folder of each edition.
Those files are no longer in this repository, so the line numbers cannot be followed.
"Not verified" means I did not read that code, or read only part of it.
I did **not** run any test. The sandbox has no `rsync`, `btrfs`, `ssh`, `sshfs`, `shellcheck`
or `textual` (checked with `which` and `import`), so neither suite can run here as it is.

---

## 1. Executive summary

1. Both apps already share layout, menu keys, SSH backend, recovery kit and PBS code. The real differences are the copy engine (rsync + hardlinks vs `btrfs send | receive`) and the package manager (apt vs pacman/AUR).
2. The fusion is one package built as a new package, with two interfaces: `CopyEngine` and `PackageManager`. Filesystem detection picks the engine.
3. The restore side takes the btrfs `RestoreEngine` (honest errors, `.bak.N`, ownership) behind the ext4 two-pane screen. The ext4 restore code is weaker on two of the README safety rules.
4. The backup side keeps each engine's on-disk layout unchanged, so existing backups stay readable.
5. Three safety gaps exist **today** and the plan fixes them first: btrfs autodetect falls back to the first mounted drive (`B:config.py:418`); ext4 never checks for a real mountpoint (`E:destinations.py:134-140`); ext4 pre-ticks every reachable destination (`E:tui_backup.py:123-131`).
6. Milestones M1-M6 touch no user data (pure logic and tests). Engines come after the safety rules are enforced and tested.
7. Everything that needs real btrfs, sudo, a USB stick or an SSH host is listed in section 7 and is marked "needs real-hardware test". None of it is claimed to work.
8. Eight decisions for Humberto are in section 8.

---

## 2. Side-by-side comparison

### 2.1 Modules

| Concern | btrfs edition (`B:`) | ext4 edition (`E:`) |
|---|---|---|
| Package name / install dir | `btrfs_restore`, `/opt/btrfs-restore-tui` (`install.sh`) | `restore_tui`, `/opt/restore-tui` (`install.sh`) |
| Backup engine | `backup.py` (1118 lines): RO snapshot, `send`/`receive`, retention, remote heal, recovery kit | `backup.py` (584): rsync `--link-dest`, retention, recovery kit |
| Low-level ops | `btrfs_ops.py` (398): argv-only wrappers for `btrfs`, `zstd`, `pv`, `ssh`; injectable | none (rsync/ssh called inline in `backup.py`, `destinations.py`) |
| Destinations abstraction | **none** (USB and remote are two methods in `backup.py`) | `destinations.py` (476): `Destination` ABC, `LocalDestination`, `RemoteDestination` |
| Restore engine | `engine.py` (768): rsync/per-file copy, conflict modes, ownership, sshfs mount, btrfs staging | `local_client.py` (150): `shutil` copy, list/browse |
| Remote browse | inside `engine.py` (`mount_remote_snapshot`) | `remote_mount.py` (75) |
| Snapshot discovery | `scanner.py` (528): local, snapper, USB legacy, manifest layout, remote | inside `local_client.py` (`list_snapshots`) |
| System state + `restore.sh` | `system_state.py` (390): pacman, AUR, flatpak, Omarchy, bootloader, systemd | `system_state.py` (390): apt, snap, flatpak, dconf, GNOME extensions, keyrings |
| Config | `config.py` (500) | `config.py` (389) |
| Backup CLI | `cli_backup.py` (300) | `cli_backup.py` (330) |
| Launch menu for backup | none | `tui_backup.py` (213) |
| Main menu | `cli.py` (232) | `cli.py` (156) |
| Restore TUI | `ui.py` (816) + `main.py` | `tui_restore.py` (393) |
| Settings TUI | `settings_ui.py` (181) | `settings_ui.py` (224) |
| PBS push / restore | `cli_pbs.py` (105), `cli_pbs_restore.py` (403) | `cli_pbs.py` (82), `cli_pbs_restore.py` (427) |
| Alerts | `notify.py` (64) | none |
| Theme | `theme.py` (273) | CSS strings repeated in three files |
| Models | `models.py` (100) | dicts |
| Uninstall | `uninstall.sh` (116) | **none** |
| Test runner | `pytest`, `run-tests.sh`, `pytest.ini` | plain scripts with module-level `assert` (e.g. `Et:test_backup.py`), run one by one |

### 2.2 Config schema

Both read `~/.config/restore-tui/config.conf` first (`B:config.py:134`, `E:config.py:83-86`), env prefix `RESTORE_TUI_`, `EXCLUDE=` lines, and `save_user_settings()` rewrites only the keys it is given (`B:config.py:430-467`, `E:config.py:353-388`).

| Key | btrfs | ext4 | Note |
|---|---|---|---|
| `TARGET_DIR`, `MAX_DISK_PERCENT`, `MIN_KEEP`, `MAX_SNAPSHOTS` | yes | yes | same names and defaults (80 / 2 / 0) |
| `TARGET_LABEL` | no (hard-coded names `USB_BTRFS`, `USB_DATA`, `B:config.py:416`) | yes (`E:config.py:134`) | merge |
| `LOCAL_SNAPSHOTS_DIR`, `STAGING_DIR`, `LOCAL_KEEP` | yes | no | btrfs-only |
| `EXCLUDE_DEFAULTS` | yes (`off` drops built-ins) | no | rsync patterns (relative to `$HOME`) vs btrfs globs (relative to each mount) are **different dialects** |
| `REMOTE_HOST`, `_PATH`, `_USER`, `_PORT`, `_NAME` | yes (default empty) | yes (**default non-empty**) | see risk R9 |
| `REMOTE_HOST_LAN`, `REMOTE_ENABLED`, `REMOTE_MAX_GB` | no | yes | rsync/ssh route logic; size budget |
| `BTRFS_REMOTE_*` legacy names | yes, deprecated (`B:config.py:107-114`) | no | keep, with warning |
| `PBS_REPOSITORY`, `PBS_PASSWORD`, `PBS_FINGERPRINT` | yes | yes | identical |
| Default hosts | none | real default host/IPs/path in code (`E:config.py:36-40`) | drop, see R9 |

README says the schema is "already aligned"; that is true for the shared keys only.

### 2.3 CLI entry points

| Command | btrfs | ext4 |
|---|---|---|
| `restore-tui` menu | `[B] [P] [X] [R] [F] [S] [Q]` (`B:cli.py:102-117`) | same keys (`E:cli.py:84-99`) |
| `restore-tui backup\|pbs\|pbs-restore\|restore\|settings` | yes | yes |
| `restore-tui --gc`, `--paths` | yes (`B:cli.py:120-169`) | **no** |
| `backup-now` | root only; re-execs under `sudo` in `bin/backup-now` | no root; direct |
| `backup-now` flags | `--dry-run --target --keep --min-keep --max-disk-percent --quiet --yes` | same plus `--only --route --remote-max-gb --menu --no-menu` |
| `restore-now` | `main.py` self-elevates with `sudo` (`main.py`) | `restore_tui.tui_restore`, unprivileged |
| Menu text for bad key | says "B, R, F or Q" (stale, `B:cli.py:204`) | "B, P, X, R, F, S or Q" |

### 2.4 TUI screens

| Screen | btrfs | ext4 |
|---|---|---|
| Restore | **one** `Tree` panel, buttons Restore / Extract / Switch snapshot / Quit (`B:ui.py:349-360`); snapshot chosen in a modal | **two panes**: snapshot timeline (35%) + explorer (65%) (`E:tui_restore.py:166-180`); USB and remote merged in one timeline (`E:tui_restore.py:272-311`) |
| Restore confirm | Backup / Overwrite / **Skip** / Cancel, flags a system restore (`B:ui.py:79-141`) | one dialog with a ".bak" checkbox **pre-ticked** and path pre-filled with `~` (`E:tui_restore.py:79-137`) |
| Progress | `ProgressModal` with live bar and error list | one status line "Restored n/total" (`E:tui_restore.py:233-253`) |
| Backup launch menu (pick destinations, route, dry run, retention) | **none** | `BackupMenuApp` (`E:tui_backup.py`) |
| Settings | drive picker + SSH remote | drive picker + SSH remote + LAN host + size budget |
| PBS restore | `Tree` of `root.pxar` + `home.pxar` | `Tree` of `home.pxar` only |

### 2.5 Features only one edition has

btrfs only: `send`/`receive` with `-p` parents and zstd stream fallback (`B:backup.py:341-411`); exclusion by deleting paths from a writable snapshot (`B:backup.py:653-670`); remote partial-copy detection and approved deletion (`B:backup.py:491-565`); `RemoteQueryError` (a failed query is never read as "empty remote", `B:backup.py:47-50, 576-615`); scratch-dir sweep after hard kill (`B:backup.py:131-171`); desktop alerts (`B:notify.py`); restore ownership rules, `.bak.N`, per-file error list, empty-dir recreation (`B:engine.py:197-515`); btrfs staging restore; Snapper scan (`B:scanner.py`); Pacman/AUR/Omarchy/bootloader capture; `uninstall.sh`; `--gc`, `--paths`; pytest suite and an opt-in loop-device end-to-end test (`Bt:test_e2e_btrfs.py`).

ext4 only: `Destination` abstraction and per-run destination picker (`E:destinations.py`, `E:tui_backup.py`); LAN-first, then WAN route with `--route` (`E:destinations.py:284-315`); remote size budget; `--link-dest` hardlink snapshots with `-aAX --numeric-ids -x` (`E:backup.py:259-270`); conservative USB autodetect with label (`E:config.py:321-341`); foreign-drive skip (`E:config.py:263-271`); APT sources/keyrings, snap, dconf, GNOME extension capture; the "software to install" checklist at the end of `restore.sh` (`E:system_state.py:249-253, 385`); two-pane restore with merged USB+remote timeline.

### 2.6 PBS backup/restore: present in both, different

| | btrfs `cli_pbs.py` / `cli_pbs_restore.py` | ext4 `cli_pbs.py` / `cli_pbs_restore.py` |
|---|---|---|
| What is pushed | newest local RO snapshots `/.snapshots/root_*` and `home_*` as `root.pxar`, `home.pxar` | live `$HOME` as `home.pxar`, **no snapshot first** |
| Root needed | yes, push and restore both refuse non-root | no |
| Exclusions | none passed to the client (the snapshot was already pruned when it was taken) | passes every `cfg.exclusions` pattern as `--exclude` |
| Restore mounts | `root.pxar` and `home.pxar` via FUSE | `home.pxar` only, rearranged into a fake `snapshots/pbs/home` layout |
| Restore engine | shared `RestoreEngine` + modals + progress | `LocalClient.restore_path`; own dialog: Backup / Overwrite / Cancel, no Skip |
| Errors shown | full list in progress modal | first 3 in a toast |
| Quit | asks for confirmation | quits at once |
| Config keys | same three | same three |

Not verified: the `proxmox-backup-client` calls themselves, the FUSE mount wait loop, and how each tree handles a wrong fingerprint.

---

## 3. Proposed architecture

### 3.1 Principles

- One package, working name `restore_fusion` (so it never collides with the old `restore_tui` import name; the commands stay `restore-tui`, `backup-now`, `restore-now`).
- **Where** (destination) is separate from **how** (copy engine) and from **what to reinstall** (package manager).
- Each engine owns its on-disk layout. Existing backups from both editions must stay readable. No migration.
- Every deletion goes through one function that requires an explicit approval object. `--yes` can never create one.
- Every external command is an argv list (the btrfs edition already does this, `B:btrfs_ops.py` docstring; the ext4 `destinations.py` builds some remote commands as shell strings with `shlex.quote`, e.g. `E:destinations.py:354, 369`; unify on argv).

### 3.2 Interfaces

```python
# engines/base.py
class CopyEngine(ABC):
    id: str                                   # "rsync" | "btrfs"
    def available(self, ctx) -> Tuple[bool, str]      # tools present? root? source fs OK?
    def sources(self, ctx) -> List[SourceSpec]        # rsync: [$HOME]; btrfs: ["/", "/home"]
    def accepts_target(self, target: TargetInfo) -> Tuple[bool, str]
    def baseline(self, dest, snapshots) -> Optional[Baseline]   # uses completed manifests of THIS engine only
    def would_be_full(self, dest) -> FullCheck        # yes | no | UNKNOWN (never "yes" on a failed query)
    def run(self, dest, ctx, emit) -> EngineResult    # completed | partial | failed
    def open_for_restore(self, snap) -> BrowseRoot    # path to browse; btrfs may need staging
    def pbs_freeze(self, ctx) -> List[PbsArchive]     # btrfs: newest RO snapshots; rsync: live $HOME (flagged non-atomic)

# destinations/base.py   (where; no copy logic)
class Destination(ABC):
    def reachable(self) -> Tuple[bool, str]           # Local: os.path.ismount(mount) AND layout checks
    def ensure_ready(self) -> Tuple[bool, str]
    def list_snapshots(self) -> List[SnapshotRef]
    def read_manifest(self, ref) / write_manifest(self, ref, data)
    def run_cmd(self, argv) -> Completed              # Local: subprocess; SSH: ssh argv
    def remove(self, ref, approval: Approval)         # no approval, no delete

# packages/base.py   (what to reinstall; distro only, no filesystem logic)
class PackageManager(ABC):
    id: str                                    # "apt" | "pacman"
    def detect(self) -> bool
    def capture(self, out_dir) -> List[str]    # writes lists, returns warnings
    def restore_fragment(self) -> str          # bash piece for restore.sh
    def leftovers_fragment(self) -> str        # bash piece that prints what could not be reinstalled
class AuxInstaller(ABC):                       # flatpak, snap, AUR helper, dconf, GNOME ext., Omarchy
    ...
```

`restore.sh` is composed from: header + engine fragment (home restore: `rsync` from `home/` vs `btrfs receive`/stream, as in `B:system_state.py` lines ~330-368) + package-manager fragment(s) + leftovers fragment. It must stay a standalone bash file with no Python and no network dependency on this app (README, both trees).

### 3.3 Filesystem detection (chooses the engine)

`detect.py`, pure functions over `findmnt` output (both trees already shell out to `findmnt`, `B:config.py:356-370`, `E:config.py:297-311`).

1. Source: fs type of `/` and `/home`, and whether each is a btrfs subvolume (`B:btrfs_ops.py` `is_subvolume`).
2. Target: fs type, and whether it is a real mountpoint (`os.path.ismount`).
3. Engine choice, `ENGINE=auto|rsync|btrfs` (default `auto`):
   - source `/` and `/home` are btrfs subvolumes and `btrfs` tool present -> `btrfs` engine;
   - otherwise -> `rsync` engine.
   - `ENGINE=` overrides only if `available()` agrees; otherwise stop with a message. Never silently switch.
4. Target rules:
   - `btrfs` engine: btrfs target -> native `receive`; other target -> `.btrfs.zst` streams (today's behaviour, `B:backup.py:681-689`).
   - `rsync` engine: target must be ext4 or btrfs (hardlinks and ownership). Today this is only a hint (`E:config.py:88-91`, `B:config.py:99-103`). Plan: **refuse** exFAT/NTFS/vfat with a clear message, because hardlink dedup would be wrong.
5. Mixed history: a manifest gets an `engine` field (manifest v2). Baseline lookup only considers snapshots of the same engine. If the target only holds the other engine's history, the run counts as **full** and goes through the existing full-backup confirmation. v1 manifests (no field) are classified by layout: a `home/` dir means rsync; `root_*`/`*.btrfs.zst` means btrfs.

Package manager detection is separate: `/etc/os-release` ID / ID_LIKE -> `apt` or `pacman`. A btrfs Ubuntu box is valid (README of the btrfs edition says "any Btrfs-root distro"), so engine and package manager are independent choices.

### 3.4 Package layout

```
restore-tui-fusion/
├── README.md  docs/PLAN_FUSION.md
├── pyproject.toml  requirements.txt  requirements-dev.txt  pytest.ini  run-tests.sh
├── bin/                          # restore-tui  backup-now  restore-now  (thin shims, same pattern as today)
├── install.sh  uninstall.sh  config.conf.example
├── restore_fusion/
│   ├── cli.py                    # menu + subcommands + --gc + --paths
│   ├── config/
│   │   ├── schema.py             # Config dataclass (union of both)
│   │   ├── loader.py             # env > file > default, legacy keys
│   │   └── store.py              # save_user_settings (in place, chmod 600)
│   ├── drives.py                 # candidate drives, ismount, foreign-layout check, fstype
│   ├── detect.py                 # source/target fs -> engine; os-release -> package manager
│   ├── model/                    # manifest.py (v1+v2), snapshot.py, restore.py (items, progress)
│   ├── safety/                   # confirm.py (default No), approval.py, retention_plan.py
│   ├── destinations/             # base.py  local.py  ssh.py   (transport + metadata only)
│   ├── engines/
│   │   ├── base.py
│   │   ├── rsync_hardlink.py
│   │   └── btrfs/                # engine.py  ops.py  remote_heal.py  staging.py
│   ├── packages/
│   │   ├── base.py  apt.py  pacman.py
│   │   ├── flatpak.py  snap.py  aur.py
│   │   └── desktop.py            # dconf, GNOME extensions, Omarchy
│   ├── system_state/             # collector.py (common), restore_script.py (composer), templates/
│   ├── backup/                   # runner.py (orchestrator), kit.py (disaster-recovery kit), scratch.py, notify.py
│   ├── restore/                  # engine.py, sources.py (USB/remote/local scan), remote_mount.py
│   ├── pbs/                      # push.py  restore.py  mount.py
│   └── ui/                       # theme.py  restore_app.py (two-pane)  backup_menu.py  settings.py  modals.py
└── tests/                        # pytest only; fakes for ops/ssh; markers: needs_rsync, needs_root_btrfs
```

---

## 4. File-by-file mapping

Verdicts: **reuse** = content copied as-is (only import paths change); **merge** = combine with the counterpart; **rewrite** = same job, new code; **drop** = not carried over.

### 4.1 btrfs edition

| File | Verdict | Goes to | Note |
|---|---|---|---|
| `.gitignore` | merge | `.gitignore` | union with ext4's |
| `README.md` | rewrite | `README.md` | one README for the fused app |
| `bin/backup-now` | merge | `bin/backup-now` | keep: sudo only when the chosen engine needs root |
| `bin/restore-now` | merge | `bin/restore-now` | same |
| `bin/restore-tui` | merge | `bin/restore-tui` | same |
| `btrfs_restore/__init__.py` | drop | - | new `__init__` |
| `btrfs_restore/backup.py` | rewrite (split) | `engines/btrfs/engine.py`, `engines/btrfs/remote_heal.py`, `backup/scratch.py`, `backup/kit.py`, `backup/runner.py` | logic kept, structure changed; recovery-kit text templates move to `backup/kit.py` |
| `btrfs_restore/btrfs_ops.py` | reuse | `engines/btrfs/ops.py` | argv-only, injectable, already tested (`Bt:test_btrfs_ops.py`) |
| `btrfs_restore/cli.py` | merge | `cli.py` | keep `--gc`, `--paths`; fix stale "Pick one" text |
| `btrfs_restore/cli_backup.py` | merge | `backup/runner.py` (CLI part in `cli.py`) | keep full-backup and heal confirmations; add destination picker |
| `btrfs_restore/cli_pbs.py` | merge | `pbs/push.py` | engine provides what to push (`pbs_freeze`) |
| `btrfs_restore/cli_pbs_restore.py` | merge | `pbs/restore.py` | shared restore engine and modals |
| `btrfs_restore/config.py` | merge | `config/*`, `drives.py` | **fix** `candidates[0]` fallback (line 418) |
| `btrfs_restore/engine.py` | merge | `restore/engine.py`, `restore/remote_mount.py`, `engines/btrfs/staging.py` | becomes the single restore engine; fix rc 23 gap (R6) |
| `btrfs_restore/models.py` | merge | `model/` | |
| `btrfs_restore/notify.py` | reuse | `backup/notify.py` | |
| `btrfs_restore/scanner.py` | merge | `restore/sources.py` | replace `StrictHostKeyChecking=no` (line 377) with `accept-new` like the rest |
| `btrfs_restore/settings_ui.py` | merge | `ui/settings.py` | take ext4's extra fields |
| `btrfs_restore/system_state.py` | merge (split) | `system_state/collector.py`, `packages/pacman.py`, `packages/aur.py`, `packages/flatpak.py`, `packages/desktop.py`, `system_state/templates/` | restore.sh text becomes composed fragments |
| `btrfs_restore/theme.py` | reuse | `ui/theme.py` | one theme for all screens |
| `btrfs_restore/ui.py` | rewrite | `ui/restore_app.py`, `ui/modals.py` | modals reused; screen layout becomes two-pane |
| `config.conf.example` | merge | `config.conf.example` | no real hosts/IPs |
| `install.sh` | merge | `install.sh` | take btrfs venv fallback + legacy retire; seed config with mode 600 (R10) |
| `main.py` | drop | - | privilege handled in `bin/` + `cli.py` |
| `pytest.ini`, `requirements*.txt`, `run-tests.sh` | reuse | repo root | |
| `uninstall.sh` | merge | `uninstall.sh` | generalize paths; fix residue check (R12) |
| `tests/test_backup.py` | merge | `tests/engines/test_btrfs_engine.py`, `tests/safety/` | |
| `tests/test_btrfs_ops.py` | reuse | `tests/engines/test_btrfs_ops.py` | |
| `tests/test_cli_confirm.py` | reuse | `tests/safety/test_confirm_remote.py` | |
| `tests/test_cli_menu.py` | merge | `tests/test_cli.py` | |
| `tests/test_cli_pbs.py` | merge | `tests/pbs/` | |
| `tests/test_config.py` | merge | `tests/config/` | |
| `tests/test_e2e_btrfs.py` | reuse | `tests/hardware/test_e2e_btrfs.py` | opt-in, real hardware |
| `tests/test_engine.py`, `test_engine_staging.py`, `test_restore_safety.py` | reuse | `tests/restore/` | |
| `tests/test_error_capture_and_alert.py` | reuse | `tests/backup/` | |
| `tests/test_remote_mount.py` | reuse | `tests/restore/` | |
| `tests/test_scanner.py`, `test_scanner_target.py` | merge | `tests/restore/test_sources.py` | |
| `tests/test_settings_ui.py`, `test_ui.py` | merge | `tests/ui/` | |
| `tests/test_system_state.py` | merge | `tests/system_state/` | shellcheck test kept (skips if missing) |

### 4.2 ext4 edition

| File | Verdict | Goes to | Note |
|---|---|---|---|
| `.gitignore` | merge | `.gitignore` | |
| `README.md` | rewrite | `README.md` | |
| `bin/*` (3) | merge | `bin/*` | |
| `restore_tui/__init__.py` | drop | - | |
| `restore_tui/backup.py` | rewrite (split) | `engines/rsync_hardlink.py`, `backup/runner.py`, `backup/kit.py` | keep rsync flags, exit-code table, retention order |
| `restore_tui/cli.py` | merge | `cli.py` | |
| `restore_tui/cli_backup.py` | merge | `backup/runner.py`, `cli.py` | `--route`, `--only` kept |
| `restore_tui/cli_pbs.py` | merge | `pbs/push.py` | live-`$HOME` push kept for the rsync engine, with `--exclude` |
| `restore_tui/cli_pbs_restore.py` | drop (replaced) | `pbs/restore.py` | uses shared restore engine; fixes the Skip / `.bak` gaps |
| `restore_tui/config.py` | merge | `config/*`, `drives.py` | keep label + foreign-drive logic; add `ismount` (R1); drop default hosts (R9) |
| `restore_tui/destinations.py` | rewrite | `destinations/{base,local,ssh}.py` | keep route selection and size budget; move `rsync_tail` into the engine |
| `restore_tui/local_client.py` | drop (replaced) | `restore/engine.py` | `.bak` deletion bug (R5) |
| `restore_tui/remote_mount.py` | merge | `restore/remote_mount.py` | |
| `restore_tui/settings_ui.py` | merge | `ui/settings.py` | |
| `restore_tui/system_state.py` | merge (split) | `system_state/collector.py`, `packages/apt.py`, `packages/snap.py`, `packages/flatpak.py`, `packages/desktop.py` | keep the leftovers checklist |
| `restore_tui/tui_backup.py` | merge | `ui/backup_menu.py` | **stop pre-ticking** (R3) |
| `restore_tui/tui_restore.py` | merge | `ui/restore_app.py` | layout kept (two-pane), engine replaced |
| `config.conf.example` | merge | `config.conf.example` | remove real hosts/IPs |
| `install.sh` | merge | `install.sh` | |
| `requirements.txt` | reuse | repo root | |
| `tests/test_backup.py` | rewrite | `tests/engines/test_rsync_engine.py` | script-style asserts become pytest; needs `rsync` |
| `tests/test_recovery_kit.py` | rewrite | `tests/backup/test_kit.py` | |
| `tests/test_remote_restore.py` | rewrite | `tests/restore/` | |
| `tests/test_settings.py` | rewrite | `tests/config/` | |
| `tests/test_system_state.py` | rewrite | `tests/system_state/` | |
| `tests/test_tui_backup.py` | rewrite | `tests/ui/` | |
| `tests/test_cli_pbs_restore.py` | rewrite | `tests/pbs/` | |

Not read in full (so mapping is by name and role only): `B:scanner.py` lines 60-160 and 345-510, `B:engine.py` 518-768, `B:theme.py`, `B:notify.py`, `B:ui.py` bodies after the modals, `E:settings_ui.py` save logic, both `restore.sh` templates, and almost all test bodies (only test names and a few tests were read).

---

## 5. Ordered milestones

Rules for every milestone: one PR into `develop`, `feature/*` branch, semantic commit, formatter before commit, tests included. Humberto merges.
Tests that need `rsync`, `btrfs`, `ssh`, root or a real USB are marked and skipped when the tool is missing. They are **not** counted as passing in this sandbox.

**Group A - cannot lose user data (pure logic, no writes outside temp dirs)**

| # | Milestone | Tests |
|---|---|---|
| M1 | Scaffolding: package skeleton, pytest config, `run-tests.sh`, markers. Port ext4 script tests to pytest *unchanged in intent*. | harness runs; one smoke test |
| M2 | `drives.py` + `detect.py`: `ismount`-based candidate drives, no first-drive fallback, foreign-layout check for **both** layouts (`restore-tui/` and `btrfs-restore/`), fstype, engine choice, package-manager choice. | stale mountpoint dir rejected; explicit unmounted `TARGET_DIR` rejected; no fallback with 2 unlabelled drives; `/mnt` not a candidate; exFAT refused for rsync; engine choice table |
| M3 | `config/`: union schema, env > file > default, legacy keys, in-place save with chmod 600, **no default host/IP/path**. | file never rewritten except by Settings; legacy keys; round trip; unknown keys preserved |
| M4 | `model/manifest.py` v1+v2 and `baseline` selection: partial/failed/running never a baseline; engine mismatch means full; `latest` moves only on `completed`. | table tests for every status x engine combination |
| M5 | `safety/`: `confirm()` defaults to No, no-tty means No; `Approval` object; `--yes` cannot build one; `plan_prune()` returns a list and deletes nothing. | `--yes` + delete = refused; EOF = No; prune plan respects `MIN_KEEP` and never the last |

**Group B - writes to user data, protected by Group A**

| # | Milestone | Tests |
|---|---|---|
| M6 | `restore/engine.py` from `B:engine.py`: `.bak.N`, Skip, ownership, per-file error list, empty dirs, fatal vs per-file. Fix the rc-23-with-empty-stderr gap. Never delete an older `.bak`. | port `Bt:test_restore_safety.py`, `test_engine.py`; new: rc 23 with empty stderr still ends "restored with errors"; existing `.bak` is kept |
| M7 | `destinations/`: Local (uses M2 mount check), SSH (argv only, timeouts, route LAN/WAN). | fake ssh runner; stale mount refused; remove needs `Approval` |
| M8 | `engines/rsync_hardlink.py` from `E:backup.py`. | port `Et:test_backup.py` (full, incremental dedup, partial, prune, restore round trip); `needs_rsync` |
| M9 | `engines/btrfs/` from `B:backup.py` + `btrfs_ops.py` with fake ops. Remote heal needs `Approval`. | port `Bt:test_backup.py` (partial, stale mount, remote base, heal, prune, scratch sweep); hardware test file kept but opt-in |
| M10 | `packages/` + `system_state/` + `restore.sh` composer. Golden tests: generated `restore.sh` for apt and for pacman must match what the source trees generate today (apart from the new fragment markers). | `bash -n`; shellcheck when present; golden diff; leftovers checklist present for **both** distros |

**Group C - assembly**

| # | Milestone | Tests |
|---|---|---|
| M11 | Backup runner + `backup-now` CLI + destination picker (nothing pre-ticked, unreachable ones disabled). Full-backup confirm kept. | Textual `Pilot` tests: no box ticked at start; Start with nothing ticked is refused |
| M12 | Restore sources + two-pane restore app on the M6 engine; merged USB+remote timeline; sshfs mount; btrfs staging. | merge logic; selection starts empty; confirm defaults |
| M13 | Settings screen merged (`ENGINE`, LAN host, size budget, drive picker with fstype warning). | save/restore round trip |
| M14 | PBS push/restore unified (`pbs_freeze`, shared restore engine). | argv tests with a fake client |
| M15 | `install.sh` / `uninstall.sh` unified; seed config mode 600; residue check that also covers a separate `/home` mount. | run both against a temp prefix (`DESTDIR`-style) and a fake `/opt`; shellcheck |
| M16 | Docs, migration notes for both editions, hardware checklist (section 7) as a runnable list. | link/lint only |

---

## 6. Risks and safety-rule check

### 6.1 Each README safety rule: preserved? which code enforces it today?

| Rule (README) | Enforced today by | Status today | In the fusion |
|---|---|---|---|
| **1. Never fall back to "first writable removable drive"; validate with a real mountpoint check** | btrfs: mountpoint check on the backup path `B:backup.py:704-720` (test `Bt:test_backup.py:275`). ext4: no-fallback autodetect `E:config.py:321-341` (test `Et:test_backup.py:123-144`) | **Broken in both, differently.** btrfs autodetect ends with `return str(candidates[0])` (`B:config.py:418`) and treats `/mnt` as removable (`B:config.py:375`). ext4 `reachable()` only checks `is_dir()` (`E:destinations.py:134-140`), `_candidate_drives` only checks writable (`E:config.py:289`); so an explicit `TARGET_DIR` that points to a left-over mount directory would write to the root disk. The ext4 foreign-drive test looks for `home`+`root`+`meta` at the mount root (`E:config.py:263-271`), but the current btrfs layout is a `btrfs-restore/` folder (`B:config.py:28`), so it likely does not match (not run) | M2 fixes it first: `ismount` is mandatory in `LocalDestination.reachable()`, no fallback, both layouts recognised, `/mnt` excluded, tests for each case |
| **2. Partial or failed runs never become the baseline or move `latest`** | ext4: `E:backup.py:86-99, 193, 206-217, 240-241`; test `Et:test_backup.py:70-78`. btrfs: `B:backup.py:187-195, 387, 392-411, 672-679`, remote base `576-615`; tests `Bt:test_backup.py:240, 438-457` | Preserved in both | Same logic in M4 + engines; new rule: baseline only from the same engine |
| **3. Restore is honest: per-file errors accumulate, "restored with errors"** | btrfs: `B:engine.py:334-515` (final state "Completed with errors", lines 504-509); tests `Bt:test_restore_safety.py:38-65`, `test_engine.py:123` | btrfs OK, with one small gap: rsync exit 23 with empty stderr adds no error and no "rsync exited" line (`B:engine.py:323-332`), so that case could end "Completed" (by reading; not run). ext4 **does not meet it**: one `OSError` aborts the rest of that item and returns `False` (`E:local_client.py:103-112`); the UI shows "Restored n/total" only (`E:tui_restore.py:233-253`) | M6 uses the btrfs engine, closes the rc-23 gap and adds a test |
| **4. Never auto-delete; `--yes` does not approve deletions; delete prompts default No** | btrfs: `B:cli_backup.py:94-98` (default No), `130-152` and the comment at `212` (`--yes` never approves), engine only deletes `heal_approved` (`B:backup.py:533-565`); tests `Bt:test_cli_confirm.py:59-90`, `test_backup.py:495-536`. `uninstall.sh`: `--yes` means "app only"; snapshots need a typed `DELETE` | Partly. **Retention pruning is automatic in both editions** with no prompt: ext4 `E:backup.py:303-359`; btrfs USB `B:backup.py:799-847`, local `849-873`, remote `627-649` (deletes remote subvolumes). Also ext4 restore silently `rmtree`s an older `.bak` before making a new one (`E:local_client.py:114-118`) | Retention kept as a *configured* feature (decision Q1), but it must go through `plan_prune()` + `Approval`, show in `--dry-run`, and keep `MIN_KEEP`. Old-`.bak` deletion is dropped (`.bak.N`, `B:engine.py:253-260`) |
| **5. The user controls the data: nothing preselected, plain controls** | btrfs restore starts with an empty selection (`B:ui.py` `selected_paths = set()`). ext4 restore starts empty (`E:tui_restore.py:164`) | **Broken in ext4**: backup picker ticks every reachable destination (`E:tui_backup.py:123-131`); restore dialog pre-ticks ".bak" and pre-fills `~` (`E:tui_restore.py:79-98`). btrfs autodetect picks a target for the user (`B:config.py:401-418`). Settings radio default: not verified | M11/M12: nothing ticked, no pre-filled target; `.bak` choice is an explicit button as in btrfs |
| **6. Data/home is sacred; apps best-effort plus an honest list of what is left** | ext4: leftovers checklist `E:system_state.py:249-253, 385`; test `Et:test_system_state.py:113` | ext4 has it. btrfs `restore.sh` prints a warning when AUR/pacman steps fail (`B:system_state.py:311, 328`) but I found no final "software to install" list (grep only; not fully verified) | M10: `leftovers_fragment()` is part of the `PackageManager` interface, so both distros must have one; a golden test checks it |

### 6.2 Other risks

| # | Risk | Evidence | Mitigation |
|---|---|---|---|
| R1 | Writing a full backup to the wrong place | rule 1 above | M2 first |
| R2 | Same stick holds `restore-tui/` and `btrfs-restore/`; each edition's baseline logic must ignore the other's snapshots | `E:config.py:263-271`, layout in `B:config.py:28` | engine tag in manifest (M4); never delete across engines |
| R3 | Pre-ticked destination + weak `reachable()` is a combined path to R1 | `E:tui_backup.py:123-131`, `E:destinations.py:134-140` | M2 + M11 |
| R4 | Package-manager abstraction changes `restore.sh`, which is the last-resort recovery path | `restore.sh` generated in both `system_state.py` | M10 golden tests against today's output; keep it standalone bash |
| R5 | Old `.bak` deleted on restore | `E:local_client.py:114-118` | M6 |
| R6 | Silent partial restore on rsync 23 | `B:engine.py:323-332` | M6 |
| R7 | Remote paths built as shell strings | `E:destinations.py:354, 369, 393` (quoted with `shlex`, but still a string) | argv-only runner (M7) |
| R8 | Host key checking off in one place | `B:scanner.py:377` (`StrictHostKeyChecking=no`); everywhere else `accept-new` | use one SSH option set |
| R9 | Real host, IPs and path are defaults in code and examples | `E:config.py:36-40`, both `config.conf.example`, placeholders in `E:settings_ui.py` / `B:settings_ui.py` | fusion ships empty defaults; users put their own values in their own config (Q5) |
| R10 | Config seeded with mode 644 can later hold `PBS_PASSWORD` | both `install.sh` (`install -m 644 ... config.conf`); `save_user_settings` only chmods 600 when it writes | seed with 600 (M15) |
| R11 | Two privilege models (btrfs: sudo everywhere; ext4: none) | `B:main.py`, `B:cli.py:36-51`, `E:cli.py` docstring | elevate per mode only when the engine needs root; the TUI itself stays unprivileged where possible |
| R12 | "Zero-residue" uninstall may report clean while files remain | `uninstall.sh` ends with `find / -xdev -name '*btrfs-restore*'`; `-xdev` stops at mount boundaries and `/home` is its own mount on a btrfs layout with a `/home` subvolume (reasoned from `B:config.py:47` `DEFAULT_SOURCE_MOUNTS`; not run). `--purge` also removes `~/.config/restore-tui`, which the ext4 edition shares | M15: search each mounted fs that matters, name the exact paths the app owns, ask before touching the shared config |
| R13 | Test suites cannot run here as they are | no `rsync`, `textual`, `btrfs` in sandbox; ext4 tests are scripts, not pytest | M1 harness; skip markers; fakes for ops/ssh |
| R14 | Text-only exclusion dialects differ (rsync patterns vs btrfs globs) | `E:config.py:48-81`, `B:config.py:56-94` | keep two lists keyed by engine; do not translate automatically |
| R15 | Mid-send kill or power loss on real hardware | handled in code (`B:backup.py:131-171, 400-411`) but only unit-tested | section 7 |

---

## 7. What cannot be verified in this sandbox

Every row: **needs real-hardware test**. None of these is claimed to work. Existing source tests that touch them either mock the tool or are opt-in.

| # | Item | Why not here | Existing test (not run) |
|---|---|---|---|
| H1 | `btrfs subvolume snapshot` / `send` / `receive`, full and incremental (`-p`), on a real btrfs | no `btrfs`, no loop-mount privilege checked | `Bt:test_e2e_btrfs.py` (opt-in, root + `mkfs.btrfs`) - needs real-hardware test |
| H2 | Writable snapshot -> delete excluded paths -> flip read-only -> send (`B:backup.py:664-668`) on nested subvolumes of a real Omarchy `/` and `/home` | needs the real subvolume layout | none - needs real-hardware test |
| H3 | `sudo` flows: re-exec in `cli.py`/`bin/`, `runuser` desktop alert, NOPASSWD `btrfs` on the remote | needs real sudoers and a user session bus | mocked in `Bt:test_error_capture_and_alert.py` - needs real-hardware test |
| H4 | USB behaviour: real mount/unmount, left-over mountpoint dir, `/run/media/<user>`, labels, hot-unplug mid-backup, exFAT/NTFS refusal | needs a physical stick | `ismount` is mocked in `Bt:test_backup.py:178, 284` - needs real-hardware test |
| H5 | SSH remote: rsync over ssh with `--link-dest`, `btrfs receive` over ssh, LAN to WAN fallback, timeouts, host-key prompt | no `ssh` binary or remote host | fake runners only - needs real-hardware test |
| H6 | `sshfs` read-only mount, restore from it, staging `receive` for a whole subvolume | no `sshfs`, no FUSE | `Bt:test_remote_mount.py` mocks it - needs real-hardware test |
| H7 | Proxmox Backup Server push, FUSE mount of `.pxar`, fingerprint handling | no `proxmox-backup-client`, no server | argv tests only - needs real-hardware test |
| H8 | `restore.sh` on a fresh Ubuntu and on an Arch live ISO (`arch-chroot`, apt/snap/flatpak, pacman/AUR, Omarchy update guard) | needs clean machines or VMs with network | `bash -n` and optional shellcheck only - needs real-hardware test |
| H9 | Ownership, ACL and xattr restore as root on real files | needs the real fs features | `Bt:test_restore_safety.py:116-144` (`TestOwnership`; body not read) - needs real-hardware test |
| H10 | Retention by real disk percent and remote size budget | needs a nearly full drive | mocked usage - needs real-hardware test |
| H11 | `install.sh` / `uninstall.sh` on a real system (`/opt`, `/usr/local/bin`, desktop entry, venv with network, residue check across mounts) | needs a disposable machine | none - needs real-hardware test |
| H12 | `SIGTERM`/`SIGKILL`/power loss mid-send; scratch sweep on real subvolumes | needs real subvolumes and a kill | `Bt:test_backup.py:742-772` (`TestBackupScratchCleanup`; body not read) - needs real-hardware test |
| H13 | Desktop notifications through `notify-send` | needs a graphical session | mocked - needs real-hardware test |
| H14 | TUI in real terminals (foot, gnome-terminal), sudo password prompt next to a TUI | Textual `Pilot` tests cover logic, not terminals | `Bt:test_ui.py` partial - needs real-hardware test |
| H15 | One stick that holds both layouts: autodetect never picks the wrong partition | needs the real stick | none - needs real-hardware test |
| H16 | Speed and memory with a real 20+ GB home on an HDD remote | needs the data | none - needs real-hardware test |

---

## 8. Open questions for Humberto

1. **Retention pruning.** Both editions delete old snapshots automatically after a good backup (limits: `MAX_DISK_PERCENT`, `MIN_KEEP`, `MAX_SNAPSHOTS`, remote budget). Does the rule "never auto-delete for the user" cover that, or only deletions you did not configure? Options: keep auto-prune as configured (my default), or ask `[y/N]` each time with `--yes` not applying.
2. **Folder names on the target.** Keep both `restore-tui/` (rsync) and `btrfs-restore/` (btrfs) so old backups stay as they are, or switch new backups to one folder name?
3. **Engine default on a btrfs machine.** `auto` picks the btrfs engine. Should the rsync engine ever be offered on a btrfs `/` (only by setting `ENGINE=rsync`), or not at all?
4. **Scope of the rsync engine.** Today it backs up `$HOME` only and relies on package lists for the rest. Keep it that way for v1?
5. **Default host and IPs.** `E:config.py:36-40` has a real default host, LAN address and path. The fusion must not ship them. You would put them in your own `config.conf`. OK?
6. **Unmounted `TARGET_DIR`.** Require a real mountpoint always, or allow a plain folder with an explicit `ALLOW_UNMOUNTED_TARGET=yes` (useful for tests and for a second internal disk)?
7. **PBS secret.** Keep `PBS_PASSWORD` in `config.conf` (mode 600, as today), or ask for it when needed and never store it?
8. **Release matrix.** Which exact systems must pass the section 7 tests before v1 (for example one Ubuntu release and one Omarchy install), and who runs them?

---

## 9. Decisions (Humberto, 2026-10-04)

The plan above is approved as written, with these answers to section 8:

1. **Retention pruning:** keep it as configured, but it goes through `plan_prune()` + `Approval`, shows in `--dry-run`, and never goes below `MIN_KEEP`.
2. **Folder names on the target:** keep both `restore-tui/` and `btrfs-restore/`. No migration of existing backups.
3. **Engine on a btrfs machine:** `ENGINE=auto` picks btrfs. rsync there only with an explicit `ENGINE=rsync`.
4. **rsync engine scope:** `$HOME` only for v1.
5. **Default host/IPs:** none shipped. Each user puts their own values in their own `config.conf`.
6. **Unmounted `TARGET_DIR`:** always require a real mountpoint. No override. Tests use fakes.
7. **`PBS_PASSWORD`:** stays in `config.conf`, mode 600, as today (unattended runs need it).
8. **Release matrix:** one Omarchy (Arch, btrfs) machine and one Ubuntu VM (ext4). Hardware tests (section 7) are run by hand by the maintainer, not by an automated agent.

Extra requirement: the fused app must be installable on the maintainer's machines and replace the earlier `btrfs-restore-tui` and `restore-tui` installs. Existing backups must keep working after the switch.

Next work: milestones M1-M5 (Group A), one PR each.
