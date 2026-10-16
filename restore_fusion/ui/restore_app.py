"""Pantalla de restauración de dos paneles (línea de tiempo | explorador), de restore-tui/tui_restore.py
con el motor de restauración de btrfs-restore-tui (M6).

  Tab       cambia de panel            Enter   abre carpeta / sube ("..") / marca un archivo
  Space     marca / desmarca           A       marca / desmarca todo lo de la vista
  R         restaurar al original      E       extraer a otra carpeta
  Q / Esc   salir (pide confirmación)

Reglas del README: NADA viene marcado (la selección empieza vacía y se vacía al cambiar de snapshot o
de área); el diálogo de confirmación no tiene un botón por defecto; el resultado dice "Restored with
errors" con la lista cuando hubo fallos.
"""

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView

from ..model.restore import ConflictResolution, RestoreItem
from ..model.snapshots import Area, Origin, Snapshot, merge_timeline
from ..restore.engine import RestoreEngine
from .modals import ConfirmExitModal, ConfirmRestoreModal, CustomPathModal, ProgressModal
from .theme import RETRO_CSS

TWO_PANE_CSS = """
#main_layout { layout: horizontal; height: 1fr; }
#left_panel { width: 36%; border: heavy #00FF66; padding: 0 1; background: #000000; }
#right_panel { width: 64%; border: heavy #00FF66; padding: 0 1; background: #000000; }
.panel_title { text-style: bold; color: #00FF66; background: #003300; padding: 0 1; margin-bottom: 1; }
.current_path { color: #00FF66; text-style: italic; margin-bottom: 1; }
ListView { background: #000000; }
ListItem { padding: 0 1; color: #00FF66; }
ListItem:focus { background: #003300; color: #00FF66; text-style: bold; }
.selected_item { color: #FFFF00 !important; text-style: bold; }
#status_line { height: 3; border-top: solid #00FF66; background: #001100; color: #00FF66; padding: 0 1; }
"""


class RestoreApp(App):
    CSS = RETRO_CSS + TWO_PANE_CSS
    TITLE = "RESTORE-TUI TIME EXPLORER"
    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("escape", "quit_app", "Quit"),
        Binding("tab", "toggle_panel", "Switch Panel"),
        Binding("space", "toggle_selection", "Select", priority=True),
        Binding("r", "restore_original", "Restore"),
        Binding("e", "extract_custom", "Extract to..."),
        Binding("a", "select_all", "Select All"),
    ]

    def __init__(
        self, services, engine: Optional[RestoreEngine] = None, *, user_home: Optional[Path] = None
    ):
        super().__init__()
        self.services = services
        self.engine = engine or RestoreEngine()
        self.user_home = Path(user_home) if user_home else Path.home()
        self.snapshots: List[Snapshot] = []
        self.active: Optional[Snapshot] = None
        self.origin: Optional[Origin] = None
        self.area: Optional[Area] = None
        self.area_root: Optional[Path] = None
        self.rel = ""
        self.entries: List[Dict] = []
        self.selected: Set[str] = set()  # NADA marcado al empezar
        self._cancelled = False

    # -- composición ----------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main_layout"):
            with Vertical(id="left_panel"):
                yield Label("SNAPSHOTS TIMELINE", classes="panel_title")
                yield ListView(id="snapshot_list")
            with Vertical(id="right_panel"):
                yield Label("GRANULAR EXPLORER", classes="panel_title")
                yield Label(
                    "Path: (pick a snapshot)", id="path_label", classes="current_path", markup=False
                )
                yield ListView(id="file_list")
        yield Label("", id="status_line", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.load_local_worker()

    def on_unmount(self) -> None:
        try:
            self.services.cleanup()
        except Exception:  # noqa: BLE001
            pass

    def set_status(self, msg: str) -> None:
        self.query_one("#status_line", Label).update(f"[ {msg} ]")

    # -- línea de tiempo ---------------------------------------------------------------------------------
    @work(thread=True)
    def load_local_worker(self) -> None:
        try:
            snaps = self.services.local_snapshots()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.set_status, f"Error: {exc}")
            snaps = []
        self.call_from_thread(self.apply_snapshots, snaps, True)
        self.load_remote_worker()

    @work(thread=True)
    def load_remote_worker(self) -> None:
        try:
            snaps, notes = self.services.remote_snapshots()
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Remote: {exc}", severity="warning")
            return
        for n in notes:
            self.call_from_thread(self.notify, n, severity="warning")
        if snaps:
            self.call_from_thread(self.apply_snapshots, snaps, False)

    def apply_snapshots(self, snaps: List[Snapshot], replace: bool) -> None:
        """Mezcla (misma clave = una entrada con varias etiquetas, p. ej. [USB+Box])."""
        base = [] if replace else self.snapshots
        self.snapshots = [s for s in merge_timeline(base, snaps) if s.usable]
        lv = self.query_one("#snapshot_list", ListView)
        lv.clear()
        for s in self.snapshots:
            flag = "" if s.status == "completed" else f"  ({s.status})"
            lv.append(ListItem(Label(f"{s.display}{flag}  [{s.tag}]", markup=False), name=s.key))
        if not self.snapshots:
            self.set_status("No usable snapshots found (USB, local or remote).")
        else:
            self.set_status(
                f"{len(self.snapshots)} snapshots. Enter on one, then browse the right panel."
            )

    def snapshot_by_key(self, key: str) -> Optional[Snapshot]:
        return next((s for s in self.snapshots if s.key == key), None)

    def select_snapshot(self, key: str) -> None:
        snap = self.snapshot_by_key(key)
        if snap is None:
            return
        self.active, self.origin = snap, snap.best_origin()
        self.area = self.area_root = None
        self.rel = ""
        self.selected.clear()  # cambiar de snapshot vacía la selección
        if self.origin is None:
            return
        if len(self.origin.areas) == 1:
            self.open_area(self.origin.areas[0])
        else:
            self.show_areas()

    def show_areas(self) -> None:
        self.entries = [{"name": a.label, "rel": f"area:{a.kind}", "is_dir": True, "area": a}
                        for a in (self.origin.areas if self.origin else [])]  # fmt: skip
        self.area = self.area_root = None
        self.rel = ""
        self._render_entries(f"{self.active.display}  [{self.origin.label}]" if self.active else "")

    # -- áreas / carpetas ----------------------------------------------------------------------------------
    def open_area(self, area: Area) -> None:
        self.area, self.rel = area, ""
        self.selected.clear()
        self._prepare_worker(self.origin, area)

    @work(thread=True)
    def _prepare_worker(self, origin: Origin, area: Area) -> None:
        modal = None
        from ..model.snapshots import Storage

        if area.storage is not Storage.TREE:
            self._cancelled = False

            def on_cancel():
                self._cancelled = True
                self.services.cancel()

            modal = ProgressModal(
                title=f"Preparing {area.label}...", can_cancel=True, on_cancel=on_cancel
            )
            self.call_from_thread(self.push_screen, modal)
        try:
            root = self.services.prepare(
                origin, area,
                (lambda t: self.call_from_thread(modal.update_progress, 0, 50, f"Receiving  {t}"))
                if modal else None,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            if modal is not None and not self._cancelled:
                self.call_from_thread(modal.update_progress, 0, 0, "", error=str(exc))
            elif modal is None:
                self.call_from_thread(self.set_status, f"Error: {exc}")
            return
        if self._cancelled:
            return
        if modal is not None:
            self.call_from_thread(modal.update_progress, 0, 100, f"Ready: {root}", done=True)
            time.sleep(0.2)
            self.call_from_thread(modal.dismiss, None)
        self.call_from_thread(self._set_root, root)

    def _set_root(self, root: Path) -> None:
        self.area_root, self.rel = root, ""
        self.load_dir("")

    def load_dir(self, rel: str) -> None:
        self.rel = rel
        base = (self.area_root / rel) if rel else self.area_root
        items: List[Dict] = []
        try:
            with os.scandir(base) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    items.append({"name": e.name, "rel": f"{rel}/{e.name}" if rel else e.name,
                                  "is_dir": is_dir, "size": size})  # fmt: skip
        except OSError as exc:
            self.set_status(f"Error listing {base}: {exc}")
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        self.entries = items
        self._render_entries(f"/{rel}" if rel else "/")

    def _render_entries(self, path_text: str) -> None:
        self.query_one("#path_label", Label).update(f"Path: {path_text}")
        fl = self.query_one("#file_list", ListView)
        keep = fl.index  # al repintar se conserva la fila: marcar/desmarcar varias veces seguidas
        fl.clear()
        if self.area is not None:
            fl.append(ListItem(Label("[..] Go up", markup=False), name=".."))
        for it in self.entries:
            mark = "[X] " if it["rel"] in self.selected else "[ ] "
            prefix = "[DIR]" if it["is_dir"] else "     "
            if it.get("area") is not None:
                mark, prefix = "    ", "[AREA]"
            size = "" if it["is_dir"] else f"  ({it.get('size', 0)} B)"
            w = ListItem(Label(f"{mark}{prefix} {it['name']}{size}", markup=False), name=it["rel"])
            if it["rel"] in self.selected:
                w.add_class("selected_item")
            fl.append(w)
        if keep is not None and keep < len(fl.children) + 0:
            fl.index = keep
        n = len(self.selected)
        self.set_status(
            f"{len(self.entries)} items, {n} selected. Space=select  Enter=open  R=restore  E=extract"
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not (event.item and event.item.name):
            return
        if event.list_view.id == "snapshot_list":
            self.select_snapshot(event.item.name)
            return
        name = event.item.name
        if name == "..":
            if self.rel:
                parts = self.rel.split("/")
                self.load_dir("/".join(parts[:-1]))
            elif self.origin and len(self.origin.areas) > 1:
                self.selected.clear()
                self.show_areas()
            return
        entry = next((e for e in self.entries if e["rel"] == name), None)
        if entry is None:
            return
        if entry.get("area") is not None:
            self.open_area(entry["area"])
        elif entry["is_dir"]:
            self.load_dir(entry["rel"])
        else:
            self.action_toggle_selection()

    # -- selección ----------------------------------------------------------------------------------------------
    def action_toggle_panel(self) -> None:
        snap, files = self.query_one("#snapshot_list", ListView), self.query_one(
            "#file_list", ListView
        )
        (files if snap.has_focus else snap).focus()

    def action_toggle_selection(self) -> None:
        child = self.query_one("#file_list", ListView).highlighted_child
        if not child or not child.name or child.name == ".." or child.name.startswith("area:"):
            return
        self.selected.symmetric_difference_update({child.name})
        self._render_entries(f"/{self.rel}" if self.rel else "/")

    def action_select_all(self) -> None:
        paths = {e["rel"] for e in self.entries if e.get("area") is None}
        if not paths:
            return
        if paths <= self.selected:
            self.selected -= paths
        else:
            self.selected |= paths
        self._render_entries(f"/{self.rel}" if self.rel else "/")

    # -- restaurar / extraer ----------------------------------------------------------------------------------------
    def _items(self) -> List[RestoreItem]:
        return self.engine.prepare_items(
            [self.area_root / r for r in sorted(self.selected)], self.area_root
        )

    def _need_selection(self) -> bool:
        if self.area is None or self.area_root is None or not self.selected:
            self.notify("Select at least one file or folder first (Space)", severity="warning")
            return False
        return True

    def action_restore_original(self) -> None:
        if not self._need_selection():
            return
        items, base = self._items(), self.area.target_base
        system = not self.engine._within_user_home(base)

        def on_confirm(res: Optional[ConflictResolution]):
            if res:
                self.run_restoration(items, base, res)

        self.push_screen(ConfirmRestoreModal(items, base, system_restore=system), on_confirm)

    def action_extract_custom(self) -> None:
        if not self._need_selection():
            return
        items = self._items()
        default = self.user_home / f"recovered_{datetime.now():%Y%m%d_%H%M%S}"

        def on_path(target: Optional[Path]):
            if not target:
                return

            def on_confirm(res: Optional[ConflictResolution]):
                if res:
                    self.run_restoration(items, target, res)

            self.push_screen(ConfirmRestoreModal(items, target, system_restore=False), on_confirm)

        self.push_screen(CustomPathModal(default), on_path)

    @work(thread=True)
    def run_restoration(
        self, items: List[RestoreItem], target_base: Path, resolution: ConflictResolution
    ):
        modal = ProgressModal(title="Restoring files...")
        self.call_from_thread(self.push_screen, modal)
        for state in self.engine.restore_generator(items, target_base, resolution):
            self.call_from_thread(modal.update_progress, state.spinner_idx, state.percent,
                                  state.current_file, state.done, state.error, state)  # fmt: skip
            time.sleep(0.01)

    def action_quit_app(self) -> None:
        self.push_screen(ConfirmExitModal(), lambda ok: self.exit() if ok else None)
