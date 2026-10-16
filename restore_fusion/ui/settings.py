"""Pantalla Settings (retro fósforo verde): motor, unidad de backup y destino SSH, sin editar
config.conf a mano. Fusión de los dos settings_ui.py (el de ext4 trae el host LAN y el presupuesto de
tamaño; el de btrfs, el selector de unidad).

  * Corre SIN privilegios: solo lee montajes y escribe el config.conf del propio usuario.
  * Solo escribe las claves que se editan aquí (`save_user_settings`: líneas ajenas, comentarios y
    EXCLUDE= quedan intactos; el archivo queda en modo 600).
  * Ningún host/IP/ruta de ejemplo real: los placeholders usan `.example`.
  * Los botones de opción muestran la configuración ACTUAL (no eligen nada por el usuario). Si la unidad
    fijada ya no está conectada, hay una opción "mantener" para no pisar ese valor en silencio.
"""

import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
)

from .. import drives as drivesmod
from ..config import load, save_user_settings
from ..config.schema import Config

SUPPORTED_TARGET_FSTYPES = {"ext4", "btrfs"}
ENGINES = ("auto", "rsync", "btrfs")
_AUTO_ID, _KEEP_ID = "target-auto", "target-keep"

TUI_CSS = """
Screen { background: #000000; color: #00FF66; }
Header { background: #002200; color: #00FF66; dock: top; text-style: bold; }
Footer { background: #002200; color: #00FF66; dock: bottom; }
#settings-body { padding: 1 2; height: 1fr; }
.section { text-style: bold; color: #00FF66; background: #003300; padding: 0 1; margin: 1 0 0 0; }
.hint { color: #00AA44; margin-bottom: 1; }
Checkbox { background: #000000; color: #00FF66; border: none; padding: 0 1; }
Checkbox:focus { background: #003300; text-style: bold; }
Checkbox:disabled { color: #006622; }
RadioSet { background: #000000; border: tall #00553a; }
RadioButton { background: #000000; color: #00FF66; border: none; padding: 0 2 0 0; }
RadioButton:focus { text-style: bold; }
RadioButton.-selected > .toggle--label { color: #FFFF00; text-style: bold; }
Input { background: #001100; color: #00FF66; border: tall #00553a; margin-bottom: 1; }
Input:focus { border: tall #FFFF00; }
Input:disabled { color: #006622; border: tall #002200; }
.lbl { color: #00AA44; margin: 0 1; }
#settings-status { color: #FFFF00; text-style: bold; margin-top: 1; height: auto; }
#actions { height: auto; margin-top: 1; }
.act_btn { margin: 0 2 0 0; background: #003300; color: #00FF66; border: tall #00FF66; }
.act_btn:focus { background: #FFFF00; color: #000000; text-style: bold; }
"""

REMOTE_INPUTS = ("host", "host-lan", "user", "path", "port", "name", "max-gb")


def settings_drives(user: str, **kw) -> List[Tuple[Path, Optional[str]]]:
    """Unidades montadas de verdad que sirven para al menos uno de los dos motores (sin repetir)."""
    seen, out = set(), []
    for engine in ("rsync", "btrfs"):
        for d, fs in drivesmod.list_backup_drives(user, engine, **kw):
            if d not in seen:
                seen.add(d)
                out.append((d, fs))
    return sorted(out)


class SettingsApp(App):
    CSS = TUI_CSS
    TITLE = "RESTORE-TUI  -  SETTINGS"
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("s", "save", "Save")]

    def __init__(
        self,
        cfg: Optional[Config] = None,
        drives: Optional[List[Tuple[Path, Optional[str]]]] = None,
        saver: Callable[[dict], Path] = save_user_settings,
    ):
        super().__init__()
        self.cfg = cfg or load()
        self.drives = drives if drives is not None else settings_drives(self.cfg.user)
        self.saver = saver
        self.saved = False
        self.saved_path: Optional[Path] = None

    # -- composición -------------------------------------------------------------------------------
    @property
    def _pinned(self) -> Optional[Path]:
        if not self.cfg.target_dir:
            return None
        return drivesmod.split_target(self.cfg.target_dir, "rsync")[0]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="settings-body"):
            yield Label("ENGINE", classes="section")
            yield Label("auto = btrfs send/receive when / and /home are btrfs subvolumes, otherwise "
                        "rsync. rsync on a btrfs system only if you choose it here.", classes="hint")  # fmt: skip
            with RadioSet(id="engine-radio"):
                for e in ENGINES:
                    yield RadioButton(e, id=f"engine-{e}", value=(self.cfg.engine == e))

            yield Label("BACKUP DESTINATION (USB / external drive)", classes="section")
            pinned = self._pinned
            listed = {d for d, _ in self.drives}
            if self.drives or pinned:
                yield Label(
                    "Pick a drive from the list. Auto-detect only chooses a drive that already holds a "
                    "backup or has the label below.", classes="hint")  # fmt: skip
                with RadioSet(id="target-radio"):
                    yield RadioButton(
                        "Auto-detect (a drive that already holds a backup, or has the label below)", id=_AUTO_ID, value=pinned is None)  # fmt: skip
                    if pinned is not None and pinned not in listed:
                        yield RadioButton(f"Keep current pin (NOT connected right now): {pinned}",
                                          id=_KEEP_ID, value=True)  # fmt: skip
                    for drive, fstype in self.drives:
                        yield RadioButton(self._drive_label(drive, fstype), id=self._drive_id(drive),
                                          value=(pinned == drive))  # fmt: skip
            else:
                yield Label("No removable drives detected right now. Plug in the right drive and reopen "
                            "Settings - it needs to be formatted ext4 or btrfs.", classes="hint")  # fmt: skip
            yield Label("Drive label used by auto-detect", classes="lbl")
            yield Input(value=self.cfg.target_label, id="input-label", placeholder="USB_DATA")

            yield Label("REMOTE (SSH) BACKUP TARGET", classes="section")
            yield Checkbox("Enable SSH remote target", value=self.cfg.remote_enabled and bool(self.cfg.remote_host),
                           id="remote-enabled")  # fmt: skip
            with Vertical(id="settings-remote-fields"):
                yield Label("Host (reachable from anywhere)", classes="lbl")
                yield Input(
                    value=self.cfg.remote_host, id="input-host", placeholder="backup-host.example"
                )
                yield Label(
                    "LAN host (tried first when reachable; blank to disable)", classes="lbl"
                )
                yield Input(
                    value=self.cfg.remote_host_lan,
                    id="input-host-lan",
                    placeholder="nas.lan.example",
                )
                yield Label("User", classes="lbl")
                yield Input(value=self.cfg.remote_user, id="input-user", placeholder=self.cfg.user)
                yield Label("Remote path", classes="lbl")
                yield Input(
                    value=self.cfg.remote_path, id="input-path", placeholder="/path/on/the/remote"
                )
                yield Label("Port", classes="lbl")
                yield Input(value=str(self.cfg.remote_port), id="input-port", placeholder="22")
                yield Label("Display name", classes="lbl")
                yield Input(
                    value=self.cfg.remote_name,
                    id="input-name",
                    placeholder="(defaults to the host)",
                )
                yield Label("Size budget for our tree, GB (0 = no cap)", classes="lbl")
                yield Input(value=str(self.cfg.remote_max_gb), id="input-max-gb", placeholder="200")
            yield Label("PROXMOX BACKUP SERVER (optional)", classes="section")
            yield Label("Only needed for [P]/[X] in the main menu. Leave blank if you do not use "
                        "Proxmox Backup Server.", classes="hint")  # fmt: skip
            yield Label("Repository (user@realm!token@host:datastore)", classes="lbl")
            yield Input(value=self.cfg.pbs_repository, id="input-pbs-repository",
                        placeholder="user@pbs!token@pbs-host.example:datastore")  # fmt: skip
            yield Label(
                "Password / token secret (hidden; blank keeps the saved one)", classes="lbl"
            )
            yield Input(value="", id="input-pbs-password", password=True,
                        placeholder="(saved)" if self.cfg.pbs_password else "")  # fmt: skip
            yield Label("Server fingerprint", classes="lbl")
            yield Input(value=self.cfg.pbs_fingerprint, id="input-pbs-fingerprint",
                        placeholder="from 'proxmox-backup-manager cert info'")  # fmt: skip
            yield Static("", id="settings-status")
        with Vertical(id="actions"):
            yield Button("[ S ] Save", id="btn-save", classes="act_btn")
            yield Button("[ Esc ] Cancel", id="btn-cancel", classes="act_btn")
        yield Footer()

    def on_mount(self) -> None:
        self._sync_remote_fields_enabled()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "remote-enabled":
            self._sync_remote_fields_enabled()

    def _sync_remote_fields_enabled(self) -> None:
        enabled = self.query_one("#remote-enabled", Checkbox).value
        for name in REMOTE_INPUTS:
            self.query_one(f"#input-{name}", Input).disabled = not enabled

    # -- unidades ---------------------------------------------------------------------------------------
    @staticmethod
    def _drive_id(drive: Path) -> str:
        return "drive-" + str(drive).replace("/", "_").replace(" ", "_")

    @staticmethod
    def _drive_label(drive: Path, fstype: Optional[str]) -> str:
        if fstype in SUPPORTED_TARGET_FSTYPES:
            return f"{drive}  [{fstype}]"
        return f"{drive}  [{fstype or 'unknown'} - format as ext4 or btrfs to use this drive]"

    def _target_choice(self) -> Optional[str]:
        """TARGET_DIR a guardar: la unidad elegida, '' = autodetectar, o lo que ya estaba fijado."""
        if not (self.drives or self._pinned):
            return str(self.cfg.target_dir or "")
        pressed = self.query_one("#target-radio", RadioSet).pressed_button
        if pressed is None or pressed.id == _KEEP_ID:
            return str(self.cfg.target_dir or "")
        if pressed.id == _AUTO_ID:
            return ""
        for drive, _ in self.drives:
            if self._drive_id(drive) == pressed.id:
                return str(drive)
        return str(self.cfg.target_dir or "")

    def _engine_choice(self) -> str:
        pressed = self.query_one("#engine-radio", RadioSet).pressed_button
        return (
            pressed.id.split("-", 1)[1] if pressed is not None and pressed.id else self.cfg.engine
        )

    # -- acciones ---------------------------------------------------------------------------------------------
    def action_cancel(self) -> None:
        self.exit(False)

    def action_save(self) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._save()
        else:
            self.exit(False)

    def _say(self, text: str) -> None:
        self.query_one("#settings-status", Static).update(text)

    def _val(self, name: str) -> str:
        return self.query_one(f"#input-{name}", Input).value.strip()

    def _save(self) -> None:
        enabled = self.query_one("#remote-enabled", Checkbox).value
        if enabled and not self._val("host"):
            self._say("[bold red]Enter a host before enabling the SSH remote target.[/]")
            return
        if enabled and not self._val("path"):
            self._say("[bold red]Enter the remote path before enabling the SSH remote target.[/]")
            return

        def num(name: str, default: int) -> Optional[int]:
            raw = self._val(name)
            if not raw:
                return default
            try:
                n = int(raw)
            except ValueError:
                return None
            return n if n >= 0 else None

        port, max_gb = num("port", 22), num("max-gb", 0)
        if port is None or max_gb is None or port == 0:
            self._say("[bold red]Port and size budget must be positive numbers.[/]")
            return
        updates = {
            "ENGINE": self._engine_choice(),
            "TARGET_DIR": self._target_choice(),
            "TARGET_LABEL": self._val("label") or "USB_DATA",
            "REMOTE_ENABLED": "true" if enabled else "false",
            "REMOTE_HOST": self._val("host"),
            "REMOTE_HOST_LAN": self._val("host-lan"),
            "REMOTE_USER": self._val("user"),
            "REMOTE_PATH": self._val("path"),
            "REMOTE_PORT": str(port),
            "REMOTE_NAME": self._val("name"),
            "REMOTE_MAX_GB": str(max_gb),
        }
        # PBS: la contraseña nunca se rellena en el campo ni se reescribe vacía; solo se envía si el
        # usuario escribió una nueva. Repo/fingerprint solo si cambiaron (no se tocan claves ajenas).
        for key, name, old in (
            ("PBS_REPOSITORY", "pbs-repository", self.cfg.pbs_repository),
            ("PBS_FINGERPRINT", "pbs-fingerprint", self.cfg.pbs_fingerprint),
        ):
            if self._val(name) != old:
                updates[key] = self._val(name)
        new_password = self.query_one("#input-pbs-password", Input).value
        if new_password:
            updates["PBS_PASSWORD"] = new_password
        try:
            path = self.saver(updates)
        except (ValueError, OSError) as exc:
            self._say(f"[bold red]Could not save: {exc}[/]")
            return
        self._say(f"[bold #00FF66]Saved to {path}[/]")
        self.saved, self.saved_path = True, path
        self.set_timer(0.8, lambda: self.exit(True))


def main() -> int:
    app = SettingsApp()
    app.run()
    return 0 if app.saved else 1


if __name__ == "__main__":
    sys.exit(main())
