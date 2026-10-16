# Real-hardware checklist (plan section 7, H1-H16)

Nothing on this list can be run by the automated test suite. A CI sandbox has no btrfs, sudo, USB
stick, SSH host, FUSE, Proxmox Backup Server or terminal. Every item below is **unverified until you tick it**.
The automated suite (`./run-tests.sh`) reports the matching tests as SKIPPED, never as passed.

Run the list on the release matrix from the plan (decision 8):

| Box | What it is | Engine |
|---|---|---|
| **A** | an Omarchy (Arch, btrfs `/` and `/home`) machine | btrfs |
| **B** | an Ubuntu VM (ext4) | rsync |
| **R** | the SSH backup host (a different machine) | - |
| **U** | a spare USB stick you can wipe (and a second one for H15) | - |

How to use it: go top to bottom, run the command in **Run** on the box in **Where**, compare with **Expect**,
write PASS / FAIL and the date in the table at the end. If an item fails, stop and open an issue with the
command, the output and the box. Do not "fix and carry on" silently.

Rules while testing: use a disposable USB stick and a disposable remote folder, never your only copy of
anything. Put host names, IPs and paths in your own `~/.config/restore-tui/config.conf` (mode 600), never in
a commit, an issue or a pull request. `<USB>` below means the stick's mount point, `<REMOTE>` your SSH target.

Install the build under test first (it is itself item H11):

```bash
cd <your clone of restore-tui-fusion> && sudo ./install.sh
restore-tui --paths          # every path the tool touches
```

---

## H1  btrfs snapshot / send / receive, full and incremental
**Where:** A (or any Linux with `btrfs-progs`, root, and the `loop` module).
**Run:**
```bash
cd <clone>
sudo env RESTORE_FUSION_E2E=1 ./.venv/bin/python -m pytest tests/hardware/test_e2e_btrfs.py -rA
```
**Expect:** `1 passed` and **no** `SKIPPED`. A skip means the prerequisites are missing; that is not a pass.
**If it fails:** attach the pytest output and `btrfs --version`.

## H2  Writable snapshot -> delete excluded paths -> read-only -> send, on nested subvolumes
**Where:** A, real `/` and `/home`.
**Run:**
```bash
sudo backup-now --only usb --dry-run          # shows the plan, writes nothing
sudo backup-now --only usb                    # real run (answer the full-backup question yourself)
sudo btrfs property get -ts /.snapshots/home_* ro | tail -n 2
ls <USB>/btrfs-restore/snapshots/*/
```
**Expect:** the dry run lists root and home as sources and writes nothing; the real run ends `completed`;
`ro=true` for the newest local snapshots; the stick holds `root` and `home` for that timestamp; none of the
built-in excluded folders (caches, trash, `node_modules`) appear inside the copied home; nested subvolumes
under `/` and `/home` are present as empty directories or copied, not as errors.
**If it fails:** run with `--quiet` and attach the log (`/.snapshots/.backup-last.log`).

## H3  sudo flows
**Where:** A, from a normal desktop terminal, then from a desktop launcher entry.
**Run:** `restore-tui` -> `B` (backup); separately `backup-now` as your normal user; then `restore-now`.
**Expect:** exactly one sudo password prompt, shown in the terminal; the menu itself runs without root; after
the prompt the TUI draws correctly; on Box B (rsync) no sudo is asked at all. Force a failure (unplug the
remote, run `backup-now --only remote`) and confirm a desktop notification appears (`notify-send` through
`runuser`) and `backup-now` exits non-zero.
On the SSH host R, if `sudo btrfs` over SSH is used, confirm the receiving user can run `sudo -n btrfs --version`.

## H4  USB behaviour
**Where:** A and B, with the physical stick U.
**Run / Expect (all must hold):**
1. Stick plugged and mounted: `backup-now --only usb --dry-run` names the stick's mount point.
2. Unmount the stick (`umount <USB>` as root), leave the empty mount-point folder in place, run
   `backup-now --only usb`: it must **refuse** ("not mounted"), and `ls <USB>` must still be empty. **Nothing**
   may be written to the root disk. This is the 24 GB incident; it is the most important line on this page.
3. Format a second stick as exFAT or NTFS: it must be refused with a message, not used.
4. Unplug the stick **during** a run (`backup-now --only usb`, pull the cable at about 30 %): the run ends
   `failed` or `partial`, no `latest` link moves, the next run still works after replugging.
5. Stick with a label different from `TARGET_LABEL` and no existing backup: not picked automatically.

## H5  SSH remote, both engines
**Where:** A (btrfs) and B (rsync), talking to R.
**Run:**
```bash
backup-now --only remote --route lan          # then --route wan, then --route auto
backup-now --only remote                       # second run = incremental
```
**Expect:** first run is announced as a FULL backup and asks first (`--yes` skips only that question); the
second is incremental (rsync: unchanged files are hardlinks, check with `stat -c %h` on a big file in the
newest and previous snapshot on R; btrfs: `btrfs receive` of a `send -p` stream). `--route lan` fails cleanly
when off the LAN; `--route auto` falls back to `wan`. A host you never connected to is accepted once
(`StrictHostKeyChecking=accept-new`) and a **changed** host key is refused. Break the connection mid-run: the
copy is reported `partial` and is never used as the baseline of the next run.

## H6  sshfs read-only browse and restore
**Where:** A and B, with at least one backup on R.
**Run:** `restore-now`, open a snapshot tagged with R's name, open a folder, mark one file, `E` (extract) to a
new folder.
**Expect:** browsing is quick and does not download the whole snapshot; the file arrives intact
(`sha256sum` against the source); `mount | grep restore-fusion-mnt` is **empty** after you quit (also after
Ctrl-C and after `kill` of the process); the mount was read-only (`touch` inside it fails). Without `sshfs`
installed on A, a whole-subvolume stage into `/.snapshots/staging` happens instead and is removed on exit.

## H7  Proxmox Backup Server
**Where:** A (push of read-only snapshots) and B (push of live `$HOME`), against a test datastore.
**Run:**
```bash
restore-tui pbs                    # [P] push; on A it needs a recent local snapshot (run [B] first)
restore-tui pbs-restore            # [X] pick a snapshot by number
```
**Expect:** A pushes `root.pxar` and `home.pxar` from the newest snapshots, no `--exclude`; B pushes
`home.pxar` from the live home with the exclusion patterns and prints the "not a snapshot" warning. A wrong
`PBS_FINGERPRINT` is refused. `pbs-restore` lists snapshots, **asks for a number (Enter alone picks nothing)**,
mounts read-only, restores a file, and afterwards `mount | grep pxar` is empty. Check that the secret never
shows in `ps aux | grep proxmox-backup-client`.

## H8  `restore.sh` on clean machines
**Where:** a fresh Ubuntu VM (apt) and an Arch live ISO or fresh Arch/Omarchy install (pacman).
**Run:** from a snapshot folder that contains `restore.sh` and `_system_state/`:
```bash
bash -n restore.sh && ./restore.sh --help
./restore.sh                       # on Arch from a live ISO: ./restore.sh --root /mnt
```
**Expect:** it needs only bash and coreutils; it asks `yes/no` before doing anything; apt: sources and
keyrings first, then packages, snap, flatpak, home; pacman: mirrors, keyring, packages, AUR helper
non-interactive, flatpak, home; Omarchy's update guard is bypassed on Omarchy only. At the end it prints the
**checklist of what is left to install by hand**. No step silently skipped; failures are listed, not hidden.

## H9  Ownership, ACLs and xattrs on restore
**Where:** A and B, as root.
**Run:** create a test tree with a file owned by another user, one with an ACL (`setfacl`) and one with an
xattr (`setfattr`), back it up, restore it with `restore-now` to the original place and to another folder.
**Expect:** owner/group, ACL and xattr survive the "original place" restore of a system path; restores into
your home end up owned by you. An unreadable file produces "restored with errors" and a list, never a plain
success. An existing file is moved aside as `.bak` (a second restore makes `.bak.1`, the first `.bak` stays).

## H10  Retention by real disk percent and remote budget
**Where:** a small stick (a few GB) on A or B; a remote folder with a small budget.
**Run:** set `MAX_DISK_PERCENT` low (for example `--max-disk-percent 30`) and `MIN_KEEP=2`, then run
`backup-now --dry-run`, then `backup-now` several times.
**Expect:** `--dry-run` lists exactly what would be pruned; the real run asks (default No) or applies only the
retention you configured; it never prunes below `MIN_KEEP`; `--yes` does not approve anything beyond that;
the newest `completed` snapshot is never removed; `--remote-max-gb 1` prunes the remote the same way.

## H11  Installer on a real system
**Where:** B (disposable VM) first, then A.
**Run:** with the **old** installs present: `sudo ./install.sh`, then `restore-tui --paths`, then later
`sudo /opt/restore-fusion/uninstall.sh`.
**Expect:**
- install: `/opt/btrfs-restore-tui` and `/opt/restore-tui` are gone from `/opt` and appear intact under
  `/opt/restore-fusion-archive/`; `/usr/local/bin/{restore-tui,restore-now,backup-now}` point into
  `/opt/restore-fusion/bin`; the venv builds; `ls -l ~/.config/restore-tui/config.conf` shows `-rw-------` if
  it was seeded; an existing config is **unchanged** (the installer asked before touching its mode);
  `/.snapshots` and the stick are untouched; old backups of **both** editions show up in `restore-now`.
- uninstall: app, links and desktop entry removed; config, snapshots and the archive kept (answer the
  prompts with the defaults); the residue report searched `/` **and** `/home` (check the "Searched these
  filesystems" line); with `/home` as a separate mount, plant `touch ~/.cache/restore-fusion-x` first and see it
  reported.

## H12  Interruption mid-send; scratch sweep
**Where:** A.
**Run:** start `sudo backup-now --only usb`; during the send run `sudo pkill -TERM -f restore_fusion.cli_backup`;
repeat with `-KILL`; finally `restore-tui --gc`.
**Expect:** TERM: the run ends `partial`/`failed`, the scratch folder `/.snapshots/.backup-tmp/<pid>-*` is
removed. KILL: the folder stays until `restore-tui --gc` (or the next run) removes it; `--gc` removes only
folders of dead pids and never a snapshot. The interrupted copy is never offered as a baseline. Repeat the
KILL test once with the power-off method on a VM if you can.

## H13  Desktop notifications
**Where:** A and B in a graphical session.
**Run:** make a backup fail on purpose (stick missing and remote down), run `backup-now` from a terminal and
from a cron/systemd-style context if you use one.
**Expect:** a critical `notify-send` banner appears and the alert marker clears after the next good run.

## H14  Real terminals
**Where:** A (foot) and B (gnome-terminal), also over SSH and inside tmux.
**Run:** `restore-tui`, then each of `B`, `R`, `S`, `P`, `X`, `F`; resize the window while a screen is open;
open the desktop launcher entry.
**Expect:** no garbled colours, no frozen screen after the sudo prompt, keys work (Tab, Space, Enter, `q`,
Esc), the confirm dialog opens with focus on **Cancel**, **nothing is ticked** on any screen until you tick
it, quitting always asks. The launcher opens the menu in a terminal.

## H15  One stick holding both layouts
**Where:** U (one stick) used by A **and** B in turn, so it ends with `btrfs-restore/` and `restore-tui/`.
**Run:** back up from A, then from B (`ENGINE=rsync` there), then on each box `backup-now --only usb --dry-run`
and `restore-now`.
**Expect:** each box uses only its own engine's folder for new backups; `restore-now` shows the snapshots of
both folders; with two sticks plugged in, a stick that already holds a backup of this engine wins over a label
match and an unrelated stick is **never** picked; with no match at all the run stops with an explanation.

## H16  Speed and memory with real data
**Where:** A or B with a 20 GB+ home, remote on an HDD.
**Run:** `/usr/bin/time -v backup-now --only remote --quiet` for a full and then an incremental run; open
`restore-now` on a big snapshot.
**Expect:** record wall time and "Maximum resident set size" in the table; the TUI stays responsive while a
backup runs; no run exhausts memory. There is no pass/fail threshold yet: write the numbers down so one can be
decided.

---

## Result table (copy, fill in, keep with the release notes)

| # | Item | Box | Date | PASS / FAIL | Notes |
|---|---|---|---|---|---|
| H1 | btrfs send/receive e2e | A | 2026-10-05 | PASS | Omarchy machine, 1 passed, no SKIPPED |
| H2 | writable -> prune -> RO -> send | A | 2026-10-05 | PARTIAL | dry-run OK; real full backup of `/` + `/home` to a loop-mounted btrfs image: completed, manifest `completed`, spot-checked file identical. `ro=true` and the prune step NOT verified |
| H3 | sudo flows, notifications on failure | A, B | | | |
| H4 | USB: unmounted refused, exFAT refused, unplug | A, B, U | | | |
| H5 | SSH remote, both engines, routes, host key | A, B, R | | | |
| H6 | sshfs browse + restore + cleanup | A, B, R | | | |
| H7 | PBS push + restore | A, B | | | |
| H8 | `restore.sh` apt + pacman | VM, live ISO | | | |
| H9 | ownership / ACL / xattr / `.bak.N` | A, B | 2026-10-05 | PARTIAL | restore engine on A: existing file kept as `.bak`, restored files identical. ACL/xattr and `.bak.N` chain NOT verified |
| H10 | retention | A or B | | | |
| H11 | install / uninstall / residue | B then A | 2026-10-05 | PARTIAL | install on A OK (old apps archived, config untouched, commands re-pointed). uninstall and box B NOT verified |
| H12 | TERM / KILL mid-send, `--gc` | A | | | |
| H13 | desktop notifications | A, B | | | |
| H14 | real terminals | A, B | | | |
| H15 | one stick, both layouts | A, B, U | | | |
| H16 | speed and memory | A or B | | | |

A release is ready when every row is PASS on the box(es) named in the plan's release matrix. Until then the
honest status of each hardware item is **needs real-hardware test**.

---

## Phase 1 items (destination wizard, phase 1)

These are separate from H1-H16 (`tests/test_docs.py` still checks exactly H1-H16). None has been run.
All are **needs real-hardware test**.

- [ ] **P1-1  `REMOTE_KEEP` on a real SSH btrfs server.**
  **Where:** box with btrfs `/` and `/home` plus an SSH server on btrfs (box R).
  **Run:** set `LOCAL_KEEP=3` and `REMOTE_KEEP=5` in a private config, run `backup-now` six times, then list
  `sudo btrfs subvolume list -o <remote path>/root` on the server and `ls /.snapshots` locally.
  **Expect:** 5 copies per kind on the server, 3 local snapshots per kind (plus any protected parent); with
  `REMOTE_KEEP` unset the server keeps as many as `LOCAL_KEEP`. Deletions only after the approval prompt.
  **Result:** | PASS / FAIL | date | notes |

- [ ] **P1-2  Local parent of an SSH baseline after USB-only runs (known gap).**
  **Where:** same box as P1-1, plus a USB btrfs drive.
  **Run:** one backup to SSH only, then more than `LOCAL_KEEP` runs to USB only, then a backup to SSH.
  **Expect:** today the SSH baseline is pruned locally and the next SSH send is a FULL send that asks for
  confirmation (strict xfail test `test_btrfs_usb_only_runs_do_not_prune_the_ssh_baseline`). Record what happens.
  **Result:** | PASS / FAIL | date | notes |

- [ ] **P1-3  Non-btrfs USB with the btrfs engine: message and `.btrfs.zst` streams.**
  **Where:** btrfs source box with an ext4 USB drive and a btrfs USB drive (box U).
  **Run:** `backup-now --dry-run` then a real `backup-now` with `TARGET_DIR` on each drive and `ENGINE=btrfs`;
  `ls <drive>/btrfs-restore/snapshots/*/`.
  **Expect:** on ext4 one message "... is ext4, not btrfs: snapshots are saved as .btrfs.zst stream files ..."
  (also in the dry run), the backup still runs and writes one `<kind>.btrfs.zst` per source; on the btrfs
  drive there is no such message. Restoring from the streams (staging on a btrfs `STAGING_DIR`) is H-level
  and NOT covered here.
  **Result:** | PASS / FAIL | date | notes |

- [ ] **P1-4  Welcome screen in a real terminal.**
  **Where:** a real terminal on box A and box B (not a pipe).
  **Run:** remove `~/.local/state/restore-tui/welcome-shown`, start `restore-tui`; press Enter; start again; press `W`.
  **Expect:** shown once on first start, not on the second, shown again on `W`; the file is listed by
  `restore-tui --paths`; `backup-now` from cron or a pipe never shows it or waits for input.
  **Result:** | PASS / FAIL | date | notes |

---

## Phase 2 items (destination wizard, phase 2)

Separate from H1-H16 and P1-*. None has been run. All are **needs real-hardware test**.

- [ ] **P2-1  Remote filesystem warning against real servers.**
  **Where:** btrfs source box plus three SSH servers: one btrfs, one ext4, one unreachable (box R, box E, a wrong host).
  **Run:** `ENGINE=btrfs backup-now --dry-run --only remote`, then a real run, once per server.
  **Expect:** btrfs server: no filesystem message. ext4 server: one warning "the remote path ... is ext2/ext3,
  not btrfs ..." (GNU `stat` prints `ext2/ext3` for ext4), in the dry run and before the send, and the send
  then fails at `btrfs receive` (the tool does not block or change engine). Unreachable: "could not determine
  the remote filesystem; continuing." Also check a not-yet-created remote path on the btrfs server (the
  check climbs to the nearest existing parent) and a server where `/bin/sh` or `stat` is unusual (BusyBox NAS).
  **Result:** | PASS / FAIL | date | notes |

- [ ] **P2-2  Menu with and without the Proxmox client.**
  **Where:** a real terminal on a box without `proxmox-backup-client` and no `PBS_*` keys, then the same box with keys.
  **Run:** start `restore-tui`; type `p` and `x` anyway; then set `PBS_REPOSITORY` and `PBS_PASSWORD` and restart.
  **Expect:** keys `[P]`/`[X]` not listed in the first case but typing them prints the existing help or the
  "optional ... Proxmox repository" message; listed in the second case. Nothing is installed by the tool.
  **Result:** | PASS / FAIL | date | notes |

- [ ] **P2-3  Settings PBS fields in a real terminal.**
  **Where:** a real terminal, user with an existing `config.conf`.
  **Run:** `restore-tui settings`, fill the PROXMOX BACKUP SERVER section including the password, save; then
  `ls -l ~/.config/restore-tui/config.conf` and `diff` against a backup of the old file; reopen Settings.
  **Expect:** the password is masked while typing and never shown again; file mode 600; only the keys you
  changed differ; a blank password field keeps the saved one.
  **Result:** | PASS / FAIL | date | notes |

- [ ] **P2-4  Destinations wizard on first run and from `[D]`.**
  **Where:** a real terminal with a real USB drive (ext4) and an exFAT stick, plus an SSH server.
  **Run:** remove `~/.local/state/restore-tui/welcome-shown`, start `restore-tui`, press Enter on the welcome;
  in the wizard try the exFAT stick, the ext4 drive (answer `n` at the summary, then `y`), then `A` for SSH;
  later press `D`. Also run `backup-now --dry-run` from cron or a pipe.
  **Expect:** nothing is preselected; the exFAT stick is flagged and cannot be picked; nothing is written until
  `y`; `TARGET_DIR` (or the `REMOTE_*` keys) appear in `config.conf` with mode 600 and other keys untouched;
  `backup-now` never shows the wizard or waits for input.
  **Result:** | PASS / FAIL | date | notes |
