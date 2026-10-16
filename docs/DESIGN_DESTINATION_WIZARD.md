# DESIGN: first-run destination wizard, clear messages, per-destination retention

Status: **design only, no code.** Written 2026-10-07 against `develop` @ `e05264c`.
Citations are `file::symbol`. "Not verified" means the code was not read or the behaviour was not run.

## 1. Why

A new user (Arch/Omarchy or Ubuntu) installs the tool and has to guess three things the code already
knows: which backup formats exist, which destinations work, and why a disk is rejected. Today:

- There is no first-run screen and no wizard (`grep` for welcome/first-run/about in `ui/` and `cli.py`: nothing).
- The engine is chosen from the **source** only (`detect.py::choose_engine`). One engine per run:
  `backup/runner.py::BackupRunner` takes a single `engine` and a list of `dests`. A run cannot send to
  one destination with btrfs and to another with rsync.
- `drives.py::validate_target` checks the destination filesystem **only for the rsync engine**
  (`ext4`/`btrfs` accepted, exFAT/NTFS/FAT refused). For the btrfs engine it checks the mountpoint but not
  the filesystem, **on purpose**: `engines/btrfs/engine.py::_send_usb` already supports a non-btrfs USB/local
  target. With btrfs it does `send | receive`; otherwise it writes `<kind>.btrfs.zst` stream files via
  `ops.send_to_stream` (see also the dry-run line, `restore/sources.py` `Storage.STREAM` and
  `engines/btrfs/staging.py`). An earlier draft of this document wrongly called that case a "likely late,
  unclear error"; it was corrected during implementation. What was missing was only a message saying so,
  which the engine now emits (phase 1).
- Remote retention for the btrfs engine uses `LOCAL_KEEP` (`engines/btrfs/engine.py::remote_for` passes
  `cfg.local_keep` to `RemoteBtrfs.keep`). `MAX_SNAPSHOTS` and `REMOTE_MAX_GB` are not consulted on that
  path (`backup/runner.py::_retain`). So the number of local parent snapshots and the number of remote
  snapshots are the same setting.

## 2. Decisions taken with the owner (2026-10-07)

1. Destinations offered by the wizard: **local/USB disk, SSH server, Proxmox Backup Server (optional)**.
2. **SMB and NFS are not offered.** The tool shows a plain notice explaining why and what to use instead (section 4).
3. **Nothing is preselected** in any list (README safety rule). Folder lists get an explicit button
   **"Mark default folders"** that the user presses if they want it. Same behaviour as Duplicati.
4. Local snapshots kept on the machine and snapshots kept per destination are **separate settings**.
5. Creating a btrfs image on a disk is a **separate, optional assistant**, not part of every backup (phase 4).
6. All UI text in English. Code comments in Spanish are fine (README working rules).

## 3. Engine and destination matrix

| Source | Destination | Engine | Needs |
|---|---|---|---|
| btrfs `/` and `/home` | USB/local btrfs | btrfs | nothing |
| btrfs | USB/local ext4 (or other non-btrfs) | btrfs | nothing: works today as `.btrfs.zst` stream files, with an info message. Restoring from streams needs a staging step on a btrfs location (`STAGING_DIR`). Optional: `ENGINE=rsync` for plain browsable copies (explicit, never silent) |
| btrfs | SSH server on btrfs | btrfs | nothing |
| btrfs | SSH server on ext4 | btrfs | **does not work**: remote send always runs `btrfs receive` on the server (`RemoteBtrfs.send` / `send_ssh_receive`; no stream fallback found in `engines/btrfs/remote.py`). Needs btrfs on the server (a btrfs image there), **or** `ENGINE=rsync` |
| not btrfs (Ubuntu) | USB/local ext4 or btrfs | rsync | nothing |
| not btrfs | SSH server ext4 or btrfs | rsync | nothing |
| any | exFAT / NTFS / FAT | none | rsync engine: refused by `drives.py::validate_target`. **btrfs engine: NOT refused by the code** (`validate_target` checks only the mountpoint; `_send_usb` would write streams, and `update_latest` needs symlinks). Phase 2 wizard flags these drives as unusable and will not offer them, but a hand-set `TARGET_DIR` is still not blocked (phase 2 errata) |

Because one run uses one engine, "USB as ext4 plus server as a btrfs image" in the same run is not possible
with the current runner. Mixing engines per destination is out of scope here; the matrix above is what the
wizard must explain.

## 4. Screens

### 4.1 Welcome (first run, and reachable later from the main menu)

Shown once, then a flag is stored in the user state (location to be decided with the path layout in
`restore-tui --paths`). Draft text:

```
Welcome to restore-fusion

BACKUP FORMATS
  - btrfs snapshots   (Arch/Omarchy, or any system whose / and /home are btrfs)
  - rsync copy        (any system; versions kept with hardlinks)

DESTINATIONS
  - USB / local disk        ext4 or btrfs
  - Another computer        over SSH
      btrfs source: the server must be btrfs, or hold a btrfs image
      ext4 source:  no special requirement
  - Proxmox Backup Server   optional

NOT SUPPORTED
  - exFAT, NTFS, FAT disks  (no hardlinks or file permissions)
  - Network shares (SMB, NFS)

[ Continue ]
```

Network-share notice (shown from the welcome screen and when the user asks for a network destination):

```
Network shares (SMB/NFS) are not used directly: they usually cannot keep Linux
permissions or hardlinks. To back up to a NAS or another computer, use an SSH
server (Settings > Remote). Advanced: place a btrfs image on the share and use it
as a normal btrfs disk.
```

NFS was **not tested** by this project; it is listed as unsupported on purpose rather than promised.

### 4.2 Destination wizard (first run, and Settings)

1. Detect: distro, package manager, is the source btrfs, is `btrfs-progs` installed.
2. "Where should backups go?" list: USB/local disk, SSH server, Proxmox Backup Server. Several can be added.
   *(Phase 2 implements the first two only, one per visit, see "Phase 2 status".)*
3. USB/local: no drive inserted -> "Insert a drive and press Enter, or choose another destination".
   Drive found -> show filesystem and apply the matrix. A non-btrfs drive is fine for the btrfs engine (stream files); say
   so, and offer the alternatives (plain copies with `ENGINE=rsync`, another drive, an image, phase 4). Never change engine silently.
4. SSH: host, user, path, connection test, detect whether the remote path is btrfs.
5. Summary, then write the config. Nothing is formatted or deleted by the wizard.

### 4.3 Folder selection

- The tool backs up `/` and `/home` and **excludes** paths (`engines/btrfs/exclusions.py`, rsync exclusions).
  The checklist is a friendly view over the existing `EXCLUDE=` list, same for both engines.
- Nothing marked by default. Button **"Mark default folders"**: Documents, Desktop, Pictures, plus the
  hidden folders people forget (`.config`, `.ssh`, `.gnupg`, application data). Unmarked: Videos/Movies, Music,
  Downloads, `.iso`/`.AppImage`/`.deb`.
- Show the size next to each folder and the estimated total next to the destination free space.

### 4.4 Phase 2 status (console wizard, `restore_fusion/wizard.py`)

Opened right after the first-run welcome and from main-menu key `[D] Destinations`. Console (`rich`), not
Textual. Never runs without a terminal and `backup-now` never imports it.

1. Lists the mounted drives found by `drives.list_backup_drives` (both engines when the engine cannot be
   determined) with filesystem and mount point. The user types a number, `A` (SSH) or `S` (skip); an empty
   answer chooses nothing. Flags: rsync engine accepts only ext4/btrfs; exFAT/NTFS/FAT are unusable with any
   engine and cannot be picked; with the btrfs engine a non-btrfs drive is allowed and annotated
   (`.btrfs.zst` streams).
2. `A`: host, user, remote path, port (Enter = 22), then the read-only `remote_fs_type` check (same as the
   backup path) and, for the btrfs engine, the warning from item D. Nothing here connects beyond that one `stat`.
3. A summary is shown and **nothing is written until `y`** (Enter = No). Only the keys shown are saved
   (`TARGET_DIR`, or `REMOTE_ENABLED/HOST/USER/PATH/PORT`) through `save_user_settings`.

Not in phase 2: Proxmox as a wizard destination (PBS stays in Settings), detecting distro / `btrfs-progs`
(step 1 of 4.2), "several destinations in one visit" (run `[D]` again), `REMOTE_HOST_LAN` / name / size budget
(still Settings), and the image offer. The wizard does not format, mount or delete anything.

Errata found in phase 2 (design vs code): the matrix row "any / exFAT-NTFS-FAT: refused" holds only for the
rsync engine; for the btrfs engine the code does not refuse it (see section 3).

## 5. Retention

- Add `REMOTE_KEEP` (SSH destinations, btrfs engine). Default = `LOCAL_KEEP` so existing configs do not change.
- `LOCAL_KEEP` then only means "local parent snapshots on this machine" (suggested value in docs: 3; the code
  floors it at 2 in `RemoteBtrfs.__init__` and `plan_local_prune`).
- USB keeps its own settings (`MAX_SNAPSHOTS`, `MAX_DISK_PERCENT`), already independent.
- Local parents are protected today for: the snapshots created in the current run, and the baseline of the
  last completed run on each **USB** destination (`runner.py::_prune_local_snapshots`). The baseline of an
  **SSH** destination is not protected explicitly. Edge case to test: the last run was USB-only, and the
  remote baseline is an older local snapshot that falls out of `LOCAL_KEEP`. Expected: the next remote send
  would become a full send, which the tool already asks to confirm.
- `REMOTE_MAX_GB` is consulted only on the rsync path (`SSHDestination.should_prune_more`, `du -sb`).
  Document it as such.

## 6. Phases and status (2026-10-07)

| # | Phase | Status |
|---|---|---|
| 1 | Clear messages and `REMOTE_KEEP` | **Done** |
| 2 | Welcome text, PBS menu, Settings wording, SSH filesystem check, destinations wizard | **Done** |
| 3 | Folder checklist with sizes and "Mark default folders" | Future improvement, no date |
| 4 | Optional image assistant: create a sparse btrfs image on an ext4/xfs/btrfs disk (local first, SSH server after), size suggested from `base + (N - 1) * average_change + margin`, mount/unmount rules, never exFAT/FAT hosts. Mounting without root via `udisksctl loop-setup` is **to verify** | Future improvement, no date |
| 5 | Network shares (SMB/NFS) | Only if a real need appears, with the warnings above |

Until phase 4 exists, a non-btrfs USB works with the btrfs engine as `.btrfs.zst` stream files, and an SSH server on
ext4 needs a btrfs image created by hand (or `ENGINE=rsync`). 
Restoring from `.btrfs.zst` streams on real hardware has not been tried (see section 8).

## 7. Open questions

- Where the first-run flag lives (user state directory vs config).
- Whether the image assistant mounts at backup time and unmounts after, or lives in `fstab` (the owner uses
  `fstab` with `nofail` on his own server; a general tool probably should not edit `fstab`).
- Whether per-destination engine selection is ever worth the complexity.

Anything that needs real disks, sudo, SSH or FUSE stays in `docs/HARDWARE_CHECKLIST.md` and is not claimed to work.

## 8. Not verified

- Restoring from `.btrfs.zst` streams on real hardware (staging receive, browsing, restore).
- SSH destinations on non-btrfs servers: concluded from reading the code (remote send is `btrfs receive` only,
  no stream fallback in `engines/btrfs/remote.py`); not run.
