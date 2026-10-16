"""Diálogos de la pantalla de restauración (de btrfs-restore-tui/ui.py).

Regla 5 del README (nada preseleccionado): en el diálogo de confirmación NINGÚN botón es el
"primario" y el foco inicial está en Cancelar, de modo que un Enter distraído no restaura nada.
Hay que elegir Backup (.bak), Overwrite o Skip a propósito.
"""

from pathlib import Path
from typing import Callable, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ProgressBar

from ..model.restore import ConflictResolution, RestoreItem
from .theme import SPINNER_FRAMES


class ConfirmRestoreModal(ModalScreen[Optional[ConflictResolution]]):
    """Qué hacer si el archivo ya existe. Cancelar es el foco inicial."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("b", "choose_bak", "Backup (.bak)"),
        Binding("o", "choose_overwrite", "Overwrite"),
        Binding("s", "choose_skip", "Skip existing"),
        Binding("c", "cancel", "Cancel"),
    ]

    def __init__(self, items: List[RestoreItem], target_path: Path, system_restore: bool = False):
        super().__init__()
        self.items = items
        self.target_path = target_path
        self.system_restore = system_restore

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose_bak(self) -> None:
        self.dismiss(ConflictResolution.BACKUP)

    def action_choose_overwrite(self) -> None:
        self.dismiss(ConflictResolution.OVERWRITE)

    def action_choose_skip(self) -> None:
        self.dismiss(ConflictResolution.SKIP)

    def on_mount(self) -> None:
        self.query_one("#btn-cancel", Button).focus()

    def compose(self) -> ComposeResult:
        total = sum(i.size_bytes for i in self.items)
        size = (
            f"{total / 1024:.1f} KB" if total < 1024 * 1024 else f"{total / (1024 * 1024):.2f} MB"
        )
        with Vertical(id="modal-dialog"):
            yield Label("CONFIRM RESTORATION", id="modal-title")
            with Vertical(id="modal-content"):
                yield Label(f"Target: {self.target_path}", markup=False)
                yield Label(f"Items to restore: {len(self.items)} ({size})", markup=False)
                if self.system_restore:
                    yield Label("\nSYSTEM restore - files keep their original owner (root). You may "
                                "need to reload services or reboot afterwards.", id="system-warning", markup=False)  # fmt: skip
                yield Label("\nIf files with matching names already exist:", markup=False)
            with Horizontal(id="modal-buttons"):
                yield Button("<B> Backup (.bak)", id="btn-bak")
                yield Button("<O> Overwrite", id="btn-overwrite")
                yield Button("<S> Skip existing", id="btn-skip")
                yield Button("<C> Cancel (Esc)", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss({
            "btn-bak": ConflictResolution.BACKUP,
            "btn-overwrite": ConflictResolution.OVERWRITE,
            "btn-skip": ConflictResolution.SKIP,
        }.get(event.button.id or ""))  # fmt: skip


class CustomPathModal(ModalScreen[Optional[Path]]):
    """Carpeta de destino para extraer."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default_path: Path):
        super().__init__()
        self.default_path = default_path

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("EXTRACT TO CUSTOM DIRECTORY", id="modal-title")
            with Vertical(id="modal-content"):
                yield Label("Enter absolute or relative destination path:")
                yield Input(value=str(self.default_path), id="input-path")
            with Horizontal(id="modal-buttons"):
                yield Button("<E> Confirm", id="btn-ok")
                yield Button("<C> Cancel (Esc)", id="btn-cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._confirm()

    def _confirm(self) -> None:
        val = self.query_one(Input).value.strip()
        if not val or Path(val).expanduser().resolve() == Path("/"):
            self.query_one(Input).value = str(self.default_path)  # nunca "" ni "/"
            return
        self.dismiss(Path(val).expanduser().resolve())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-ok":
            self._confirm()
        else:
            self.dismiss(None)


class ProgressModal(ModalScreen[None]):
    """Spinner + barra. El estado final es HONESTO: 'Restored with errors' con la lista, nunca un
    'completed' pelado cuando hubo fallos (regla 3 del README)."""

    BINDINGS = [
        Binding("escape", "handle_key_dismiss", "Close / Cancel"),
        Binding("c", "handle_key_dismiss", "Close / Cancel"),
        Binding("enter", "handle_key_dismiss", "Close"),
    ]

    def __init__(self, title: str = "Restoring files...", can_cancel: bool = False,
                 on_cancel: Optional[Callable[[], None]] = None):  # fmt: skip
        super().__init__()
        self.title_text, self.can_cancel, self.on_cancel = title, can_cancel, on_cancel
        self.is_done = False
        self.final_text = ""

    def action_handle_key_dismiss(self) -> None:
        if self.is_done:
            self.dismiss(None)
        elif self.can_cancel:
            self.trigger_cancel()

    def trigger_cancel(self) -> None:
        if self.on_cancel:
            self.on_cancel()
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="progress-box"):
            yield Label(f"[ | ] {self.title_text}", id="progress-spinner", markup=False)
            yield ProgressBar(total=100, show_eta=False, id="progress-bar")
            yield Label("", id="progress-filename", markup=False)
            with Horizontal(id="modal-buttons"):
                if self.can_cancel:
                    yield Button(Content("[ <C> Cancel ]"), id="btn-cancel")
                btn = Button(Content("[ <Enter> Close ]"), id="btn-done")
                btn.display = False
                yield btn

    def update_progress(self, spinner_idx: int, percent: int, current_file: str, done: bool = False,
                        error: Optional[str] = None, state=None):  # fmt: skip
        if self.is_done and not (done or error):
            return  # un cuadro de spinner tardío no pisa un mensaje final
        spinner = self.query_one("#progress-spinner", Label)
        bar = self.query_one("#progress-bar", ProgressBar)
        fname = self.query_one("#progress-filename", Label)
        btn_done = self.query_one("#btn-done", Button)

        def finish():
            if self.can_cancel:
                try:
                    self.query_one("#btn-cancel", Button).display = False
                except Exception:  # noqa: BLE001
                    pass
            btn_done.display = True
            btn_done.focus()

        restored = getattr(state, "processed_files", None)
        total = getattr(state, "total_files", None)
        failed = getattr(state, "failed_files", 0)
        fatal = getattr(state, "fatal", False)
        if error and done and not fatal:
            self.is_done = True
            head = "Restored with errors"
            if restored is not None and total is not None:
                head += f"  ({restored}/{total} ok, {failed} failed)"
            self.final_text = head
            spinner.update(head)
            bar.progress = percent
            fname.update(error)
            finish()
            return
        if error:
            self.is_done = True
            self.final_text = "Error Encountered"
            spinner.update("Error Encountered")
            bar.progress = 0
            fname.update(error)
            finish()
            return
        if done:
            self.is_done = True
            self.final_text = "Operation completed successfully"
            spinner.update("Operation completed successfully")
            bar.progress = 100
            fname.update("All files restored.")
            finish()
            return
        spinner.update(
            f"{SPINNER_FRAMES[spinner_idx % len(SPINNER_FRAMES)]} {self.title_text} [ {percent}% ]"
        )
        bar.progress = percent
        fname.update(current_file)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-done":
            self.dismiss(None)
        elif event.button.id == "btn-cancel":
            self.trigger_cancel()


class ConfirmExitModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "confirm", "Quit"),
        Binding("y", "confirm", "Yes"),
    ]

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("QUIT RESTORE-TUI", id="modal-title")
            with Vertical(id="modal-content"):
                yield Label("Are you sure you want to quit the application?", id="exit-msg")
            with Horizontal(id="modal-buttons"):
                yield Button("<Q> Quit", id="btn-exit-yes")
                yield Button("<C> Cancel (Esc)", id="btn-exit-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-exit-yes")
