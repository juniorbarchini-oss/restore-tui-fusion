"""Motor de restauración: copia archivos/carpetas de un snapshot a su destino.

De btrfs-restore-tui/engine.py (la parte de copia; el montaje sshfs y el staging btrfs
llegan en M12). Reglas del README que cumple:

  * Honesto (regla 3): un error por archivo se acumula y el estado final lo dice
    ("Completed with errors"); nunca un "Completed" pelado. Un rsync que sale con 23 sin
    detalle TAMBIÉN termina con error (fix R6). Un error fatal (destino no escribible,
    disco lleno) aborta de inmediato.
  * Nunca se borra un .bak anterior (regla 4): el respaldo va a `.bak`, `.bak.1`, `.bak.2`...
    También en la ruta de carpetas con rsync: antes de copiar se mueven aparte los archivos
    que rsync cambiaría (rsync --backup pisaría un `.bak` viejo).
  * Overwrite nunca borra un directorio para poner un archivo en su lugar.
  * Dueños: en $HOME todo queda del usuario; en rutas del sistema cada archivo conserva el
    dueño del snapshot.

Todo comando externo es una lista argv.
"""

import errno
import getpass
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Generator, List, Optional

from ..model.restore import ConflictResolution, RestoreItem, RestoreProgress

# campos del out-format separados por "|": itemize y largo nunca lo contienen, y el nombre va
# al final (split con maxsplit=2). OJO: rsync escapa los caracteres de control (\x1f -> \#037),
# así que el separador original de btrfs-restore-tui nunca parseaba y el progreso no se veía.
_RSYNC_SEP = "|"
_RSYNC_FMT = f">>%i{_RSYNC_SEP}%l{_RSYNC_SEP}%n"


class _FatalRestore(RuntimeError):
    """Condición irrecuperable dentro del helper rsync (disco lleno): aborta todo con fatal=True."""


def _drain(stream, sink: List[str]) -> threading.Thread:
    """Lee stderr en un hilo: evita el bloqueo si rsync escribe más que el pipe mientras
    leemos stdout."""

    def run():
        try:
            sink.append(stream.read() or "")
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


class RestoreEngine:
    def __init__(
        self,
        progress_cb: Optional[Callable[[str], None]] = None,
        *,
        user: Optional[str] = None,
        home: Optional[Path] = None,
        target_uid: Optional[int] = None,
        target_gid: Optional[int] = None,
    ):
        self._active_procs: List[subprocess.Popen] = []
        self._progress_cb = progress_cb
        # uid/gid reales del usuario (también bajo sudo)
        sudo_uid, sudo_gid = os.getenv("SUDO_UID"), os.getenv("SUDO_GID")
        if target_uid is not None and target_gid is not None:
            self.target_uid, self.target_gid = target_uid, target_gid
        elif sudo_uid and sudo_gid:
            self.target_uid, self.target_gid = int(sudo_uid), int(sudo_gid)
        else:
            self.target_uid, self.target_gid = os.getuid(), os.getgid()
        self._user = user or os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        self._home = Path(home) if home is not None else Path(f"/home/{self._user}")

    def cancel_active_operation(self) -> None:
        self._kill_active_procs()

    def _kill_active_procs(self) -> None:
        """SIGTERM al grupo de cada etapa y SIGKILL a lo que siga vivo."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            alive = [p for p in self._active_procs if p.poll() is None]
            if not alive:
                break
            for proc in alive:
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.send_signal(sig)
                    except OSError:
                        pass
            for proc in alive:
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
        self._active_procs = []

    def _within_user_home(self, p: Path) -> bool:
        """True si `p` es el home del usuario o algo dentro."""
        try:
            home = self._home
            p = Path(p).resolve() if p.exists() else Path(p)
            return p == home or home in p.parents
        except (OSError, ValueError):
            return False

    def _apply_ownership(self, path: Path, *, to_user: bool, src: Optional[Path] = None) -> None:
        """Set ownership on a restored path.

        Restoring into the user's home -> give it to the user (files copied as
        root otherwise). Restoring to `/` or another system path -> keep the
        owner recorded in the snapshot (`src`), so e.g. /etc/sudoers stays
        root:root; if `src` is unknown, leave it as created (root)."""
        try:
            if to_user:
                os.chown(path, self.target_uid, self.target_gid, follow_symlinks=False)
            elif src is not None:
                st = src.stat(follow_symlinks=False)
                os.chown(path, st.st_uid, st.st_gid, follow_symlinks=False)
        except (PermissionError, ProcessLookupError, OSError, FileNotFoundError):
            pass

    def prepare_items(self, selected_paths: List[Path], snapshot_root: Path) -> List[RestoreItem]:
        """Convert selected paths into RestoreItem list with calculated byte sizes."""
        items: List[RestoreItem] = []

        for p in selected_paths:
            if not p.exists():
                continue

            try:
                rel = p.relative_to(snapshot_root)
            except ValueError:
                rel = Path(p.name)

            if p.is_file() or p.is_symlink():
                size = p.stat().st_size if not p.is_symlink() else 0
                items.append(
                    RestoreItem(source_path=p, is_dir=False, size_bytes=size, rel_path=rel)
                )
            elif p.is_dir():
                total_size = 0
                for root, _, files in os.walk(p):
                    for f in files:
                        fp = Path(root) / f
                        try:
                            if not fp.is_symlink():
                                total_size += fp.stat().st_size
                        except (OSError, PermissionError):
                            pass
                items.append(
                    RestoreItem(source_path=p, is_dir=True, size_bytes=total_size, rel_path=rel)
                )

        return items

    def _get_backup_path(self, target: Path) -> Path:
        """Ruta de respaldo que no pisa nada: `.bak`, `.bak.1`, `.bak.2`..."""
        bak = target.with_name(f"{target.name}.bak")
        idx = 1
        while bak.exists() or bak.is_symlink():
            bak = target.with_name(f"{target.name}.bak.{idx}")
            idx += 1
        return bak

    # -- copia rápida de directorios con rsync -----------------------------

    def _rsync_argv(
        self,
        src_dir: Path,
        dest_dir: Path,
        resolution: ConflictResolution,
        to_user: bool,
        *,
        dry_run: bool = False,
    ) -> List[str]:
        root = os.geteuid() == 0
        argv = ["rsync", "-rlptD", "--out-format=" + _RSYNC_FMT]
        if root:
            # -A/-X y dueño/grupo solo tienen sentido como root
            argv += ["-AXH", "-S", "-go", "--numeric-ids"]
            if to_user:
                argv.append(f"--chown={self.target_uid}:{self.target_gid}")
        if resolution == ConflictResolution.SKIP:
            argv.append("--ignore-existing")
        # BACKUP: NO se usa `--backup --suffix=.bak` (pisaría un .bak anterior); ver _backup_aside
        if dry_run:
            argv.append("-n")
        argv += ["--", f"{src_dir}/", f"{dest_dir}/"]
        return argv

    @staticmethod
    def _parse_out_line(line: str):
        if not line.startswith(">>"):
            return None
        try:
            itemize, length, name = line[2:].rstrip("\n").split(_RSYNC_SEP, 2)
        except ValueError:
            return None
        return itemize, length, name

    def _backup_aside(self, src_dir: Path, dest_dir: Path, to_user: bool):
        """Mueve a `.bak[.N]` los archivos del destino que rsync va a reemplazar.

        Usa un rsync en seco (-n) con las mismas opciones para saber cuáles cambiarían; los
        archivos nuevos y los idénticos no se tocan. Devuelve (moved, error): si el ensayo
        falla, `error` explica y NO se copia nada de este ítem (nunca sobrescribir sin respaldo).
        """
        argv = self._rsync_argv(src_dir, dest_dir, ConflictResolution.BACKUP, to_user, dry_run=True)
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.SubprocessError) as exc:
            return [], f"could not plan the backup of existing files: {exc}"
        if res.returncode not in (0, 24):
            detail = (res.stderr or "").strip().splitlines()[:1]
            return [], (
                f"could not plan the backup of existing files (rsync exit {res.returncode})"
                + (f": {detail[0]}" if detail else "")
            )
        moved = []
        for line in res.stdout.splitlines():
            parsed = self._parse_out_line(line)
            if not parsed:
                continue
            itemize, _length, name = parsed
            # recibe un archivo/enlace que ya existía (no es "+++++++++" = nuevo)
            if len(itemize) < 3 or itemize[0] not in ">c" or itemize[1] not in "fLDS":
                continue
            if "+" in itemize[2:]:
                continue
            existing = dest_dir / name
            if not (existing.exists() or existing.is_symlink()) or existing.is_dir():
                continue
            bak = self._get_backup_path(existing)
            try:
                shutil.move(str(existing), str(bak))
            except OSError as exc:
                return moved, f"could not back up {name}: {exc.strerror or exc}"
            if to_user:
                self._apply_ownership(bak, to_user=True)
            moved.append(name)
        return moved, None

    def _rsync_item(
        self, src_dir: Path, dest_dir: Path, resolution: ConflictResolution, to_user: bool
    ):
        """Copia `src_dir` a `dest_dir` con rsync. Emite ('progress', n_files, n_bytes, last_name)
        y ('error', message). Lanza _FatalRestore en ENOSPC."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        if resolution == ConflictResolution.BACKUP:
            _moved, err = self._backup_aside(src_dir, dest_dir, to_user)
            if err:
                yield ("error", err)
                return
        argv = self._rsync_argv(src_dir, dest_dir, resolution, to_user)
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._active_procs = [proc]
        err_sink: List[str] = []
        err_thread = _drain(proc.stderr, err_sink)
        n_files = n_bytes = 0
        last_name = ""
        try:
            for line in proc.stdout:
                parsed = self._parse_out_line(line)
                if not parsed:
                    continue
                itemize, length, name = parsed
                if len(itemize) > 1 and itemize[1] in ("f", "L"):  # archivo / enlace
                    n_files += 1
                    try:
                        n_bytes += int(length)
                    except ValueError:
                        pass
                    last_name = name.rsplit("/", 1)[-1]
                    if n_files >= 64:
                        yield ("progress", n_files, n_bytes, last_name)
                        n_files = n_bytes = 0
            proc.wait()
        finally:
            self._active_procs = []
        if n_files:
            yield ("progress", n_files, n_bytes, last_name)

        err_thread.join(timeout=5.0)
        err = ("".join(err_sink)).strip()
        rc = proc.returncode
        if rc in (0, 24):  # 24 == archivos de origen desaparecieron durante la corrida
            return
        if rc == 11 or "No space left" in err:
            raise _FatalRestore(f"Disk full while restoring {dest_dir.name}")
        lines = [e for e in err.splitlines() if e][:8]
        for eline in lines:
            yield ("error", eline)
        if rc == 23 and not lines:
            # transferencia parcial SIN detalle: igual cuenta como error (nunca "Completed")
            yield ("error", f"rsync reported a partial transfer (exit 23) for {src_dir.name}")
        elif rc != 23:
            yield ("error", f"rsync exited {rc} for {src_dir.name}")

    def restore_generator(
        self,
        items: List[RestoreItem],
        target_base: Path,
        resolution: ConflictResolution = ConflictResolution.BACKUP,
        preserve_system_ownership: Optional[bool] = None,
    ) -> Generator[RestoreProgress, None, None]:
        """
        Execute file restoration incrementally, yielding progress states
        to drive the ASCII spinner and progress bar.

        A per-file error (permission, a vanished source) is collected and the
        run continues; the final state carries `done=True` together with an
        `error` summary and `failed_files` - never a bare "completed". A fatal
        error (target not writable, disk full) aborts at once with
        `done=True, fatal=True`.

        `preserve_system_ownership`: None -> decide from the target (user home =
        chown to user, anywhere else = keep the snapshot's owner). True/False
        forces it.
        """
        target_base = Path(target_base)
        if preserve_system_ownership is None:
            to_user = self._within_user_home(target_base)
        else:
            to_user = not preserve_system_ownership

        total_bytes = sum(item.size_bytes for item in items)

        def _state(**kw):
            base = dict(
                total_files=total_files,
                processed_files=processed_files,
                total_bytes=total_bytes,
                processed_bytes=processed_bytes,
                current_file="",
                spinner_idx=spinner_frame,
                failed_files=len(errors),
                errors=list(errors),
            )
            base.update(kw)
            return RestoreProgress(**base)

        total_files = 0
        processed_files = 0
        processed_bytes = 0
        spinner_frame = 0
        errors: List[str] = []

        # -- fatal preflight: is the target base usable at all? --
        try:
            target_base.mkdir(parents=True, exist_ok=True)
            probe = target_base / ".restore-fusion-write-test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            yield _state(
                current_file="",
                done=True,
                fatal=True,
                error=f"Cannot write to {target_base}: {exc}",
            )
            return

        use_rsync = shutil.which("rsync") is not None
        dir_items = [it for it in items if it.is_dir]

        all_files_to_copy = []
        dirs_to_make = []  # (src_dir, rel) - recreated even when empty
        for item in items:
            if item.is_dir:
                dirs_to_make.append((item.source_path, item.rel_path))
                for root, subdirs, files in os.walk(item.source_path):
                    try:
                        base_rel = Path(root).relative_to(item.source_path)
                    except ValueError:
                        continue
                    for d in subdirs:
                        dirs_to_make.append((Path(root) / d, item.rel_path / base_rel / d))
                    for f in files:
                        all_files_to_copy.append((Path(root) / f, item.rel_path / base_rel / f))
            else:
                all_files_to_copy.append((item.source_path, item.rel_path))

        total_files = len(all_files_to_copy)
        yield _state(current_file="Preparing...")

        # -- recreate the directory tree first, so empty dirs survive a restore
        #    and every level gets the right owner (not just each file's parent) --
        for src_dir, rel in dirs_to_make:
            dest_dir = target_base / rel
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                self._apply_ownership(dest_dir, to_user=to_user, src=src_dir)
            except OSError as exc:
                errors.append(f"{rel}/: {exc.strerror or exc}")

        # -- directory selections: one rsync each (fast, carries ACLs / xattrs /
        #    hardlinks / sparse regions). Standalone files stay on the per-file
        #    path below so its conflict + error handling is unchanged. --
        if use_rsync and dir_items:
            for item in dir_items:
                dest_dir = target_base / item.rel_path
                try:
                    for ev in self._rsync_item(item.source_path, dest_dir, resolution, to_user):
                        if ev[0] == "progress":
                            _, n_files, n_bytes, name = ev
                            processed_files += n_files
                            processed_bytes += n_bytes
                            spinner_frame = (spinner_frame + 1) % 4
                            yield _state(current_file=name)
                        else:
                            errors.append(f"{item.rel_path}: {ev[1]}")
                            yield _state(current_file=f"! {item.rel_path.name}")
                except _FatalRestore as exc:
                    yield _state(
                        current_file="",
                        done=True,
                        fatal=True,
                        error=f"{exc} - aborted with " f"{processed_files}/{total_files} done",
                    )
                    return
            files_to_process = [(it.source_path, it.rel_path) for it in items if not it.is_dir]
        else:
            files_to_process = all_files_to_copy

        for src, rel in files_to_process:
            dest = target_base / rel
            try:
                # -- existing-file conflict --
                if dest.exists() or dest.is_symlink():
                    if resolution == ConflictResolution.SKIP:
                        processed_files += 1
                        if src.exists() and not src.is_symlink():
                            processed_bytes += src.stat().st_size
                        continue
                    if resolution == ConflictResolution.BACKUP:
                        bak_path = self._get_backup_path(dest)
                        shutil.move(str(dest), str(bak_path))
                        if to_user:
                            self._apply_ownership(bak_path, to_user=True)
                    elif resolution == ConflictResolution.OVERWRITE:
                        if dest.is_dir() and not dest.is_symlink():
                            # nunca se borra un directorio entero para poner un archivo
                            # (Overwrite = reemplazar archivos, no destruir árboles)
                            raise IsADirectoryError(
                                errno.EISDIR,
                                "a directory is in the way; not replacing it with a file "
                                "(choose Backup to move it aside)",
                            )
                        dest.unlink()

                dest.parent.mkdir(parents=True, exist_ok=True)
                self._apply_ownership(dest.parent, to_user=to_user, src=src.parent)

                # -- copy --
                if src.is_symlink():
                    link_target = os.readlink(src)
                    if dest.exists() or dest.is_symlink():
                        dest.unlink()
                    os.symlink(link_target, dest)
                    file_size = 0
                else:
                    shutil.copy2(src, dest)
                    file_size = src.stat().st_size

                self._apply_ownership(dest, to_user=to_user, src=src)
                processed_bytes += file_size
                processed_files += 1
                spinner_frame = (spinner_frame + 1) % 4
                yield _state(current_file=rel.name)

            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    errors.append(f"{rel}: disk full")
                    yield _state(
                        current_file=rel.name,
                        done=True,
                        fatal=True,
                        error=f"Disk full while restoring {rel.name} - "
                        f"aborted with {processed_files}/{total_files} done",
                    )
                    return
                errors.append(f"{rel}: {exc.strerror or exc}")
                yield _state(current_file=f"! {rel.name}")
            except Exception as exc:  # noqa: BLE001 - report, don't crash the run
                errors.append(f"{rel}: {exc}")
                yield _state(current_file=f"! {rel.name}")

        if errors:
            shown = "\n".join(errors[:8])
            if len(errors) > 8:
                shown += f"\n... and {len(errors) - 8} more"
            yield _state(
                current_file="Completed with errors",
                done=True,
                error=f"{len(errors)}/{total_files} file(s) failed:\n{shown}",
            )
        else:
            # rsync's transferred-file count can differ slightly from the walk
            # count (unchanged files, dir entries); land the bar at 100%.
            processed_files = max(processed_files, total_files)
            processed_bytes = max(processed_bytes, total_bytes)
            yield _state(current_file="Completed", done=True)
