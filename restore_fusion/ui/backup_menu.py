"""Menú de lanzamiento de `backup-now` (retro fósforo verde). De restore-tui/tui_backup.py.

Elegir destinos, ruta del remoto, dry-run y retención; luego B (o el botón Start).

NADA viene preseleccionado (regla 5 del README): cada destino alcanzable aparece HABILITADO y SIN
marcar; el usuario marca los que quiere. Los inalcanzables quedan deshabilitados con el motivo.
Start sin marcar nada se rechaza. (El ext4 original marcaba solo todo lo alcanzable.)
"""

from typing import List, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, RadioButton, RadioSet

from ..config.schema import Config
from ..destinations.ssh import SSHDestination

TUI_CSS = """
Screen { background: #000000; color: #00FF66; }
Header { background: #002200; color: #00FF66; dock: top; text-style: bold; }
Footer { background: #002200; color: #00FF66; dock: bottom; }
#body { padding: 1 2; height: 1fr; }
.section { text-style: bold; color: #00FF66; background: #003300; padding: 0 1; margin: 1 0 0 0; }
.hint { color: #00AA44; margin-bottom: 1; }
Checkbox { background: #000000; color: #00FF66; border: none; padding: 0 1; }
Checkbox:focus { background: #003300; text-style: bold; }
Checkbox.-on > .toggle--label { color: #FFFF00; text-style: bold; }
Checkbox:disabled { color: #006622; }
#retention { height: auto; margin-top: 1; }
#retention Input { width: 18; margin: 0 1; background: #001100; color: #00FF66; border: tall #00553a; }
#retention Input:focus { border: tall #FFFF00; }
.lbl { color: #00AA44; margin: 0 1; }
RadioSet { background: #000000; border: none; layout: horizontal; height: auto; }
RadioButton { background: #000000; color: #00FF66; border: none; padding: 0 2 0 0; }
RadioButton:focus { text-style: bold; }
RadioButton.-selected > .toggle--label { color: #FFFF00; text-style: bold; }
#status_bar { height: 3; border-top: solid #00FF66; background: #001100; color: #00FF66; padding: 0 1; margin-top: 1; }
#actions { height: auto; margin-top: 1; }
.act_btn { margin: 0 2 0 0; background: #003300; color: #00FF66; border: tall #00FF66; }
.act_btn:focus { background: #FFFF00; color: #000000; text-style: bold; }
"""


class ActButton(Button):
    """Botón que responde a Enter y a Espacio (el Button base solo atiende Enter)."""

    BINDINGS = [Binding("enter,space", "press", show=False)]


class NumInput(Input):
    """Campo numérico: Input consume toda tecla imprimible, lo que taparía B y Q.
    Aquí solo entran dígitos, así que B/Q no se las queda y suben al App."""

    def check_consume_key(self, key: str, character: Optional[str]) -> bool:
        if key in ("b", "q"):
            return False
        return super().check_consume_key(key, character)


class RouteSet(RadioSet):
    """RadioSet horizontal: ←/→ cambian de opción; ↑/↓ salen hacia el widget vecino."""

    BINDINGS = [
        Binding("down", "app.focus_next", show=False),
        Binding("up", "app.focus_previous", show=False),
    ]


class BackupMenuApp(App):
    CSS = TUI_CSS
    TITLE = "BACKUP-NOW - LAUNCH MENU"
    BINDINGS = [
        # priority: B y Q valen desde cualquier widget (también sobre un campo de retención)
        Binding("b", "start", "Start backup", priority=True),
        Binding("q", "quit", "Quit", priority=True),
        Binding("down,right", "focus_next", "Next", show=False),
        Binding("up,left", "focus_previous", "Previous", show=False),
        Binding("escape", "quit", "Quit"),
    ]

    def __init__(self, cfg: Config, candidates: List):
        super().__init__()
        self.cfg = cfg
        self.candidates = candidates
        self.reach: dict = {}
        self.result: Optional[dict] = None
        self._remote_idx = next(
            (i for i, d in enumerate(candidates) if isinstance(d, SSHDestination)), None
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="body"):
            yield Label("DESTINATIONS", classes="section")
            yield Label(
                "Nothing is selected. Space ticks the ones you want.",
                classes="hint",
                id="dest_hint",
            )
            for i, d in enumerate(self.candidates):
                yield Checkbox(
                    f"{d.label}   {d.display_root()}   (probing...)",
                    value=False,
                    id=f"dest_{i}",
                    disabled=True,
                )
            if self._remote_idx is not None:
                yield Label("REMOTE ROUTE", classes="section")
                yield Label("LAN = fast at home; WAN = works anywhere", classes="hint")
                with RouteSet(id="route"):
                    yield RadioButton("Auto  (LAN, fall back to WAN)", value=True, id="route_auto")
                    yield RadioButton("LAN only", id="route_lan")
                    yield RadioButton("WAN only", id="route_wan")
            yield Label("OPTIONS", classes="section")
            yield Checkbox("Dry run  -  probe only, write nothing", value=False, id="opt_dryrun")
            yield Label("RETENTION", classes="section")
            with Horizontal(id="retention"):
                yield Label("USB max disk %", classes="lbl")
                yield NumInput(str(self.cfg.max_disk_percent), id="in_pct", type="integer")
                yield Label("remote max GB", classes="lbl")
                yield NumInput(str(self.cfg.remote_max_gb), id="in_gb", type="integer")
                yield Label("min keep", classes="lbl")
                yield NumInput(str(self.cfg.min_keep), id="in_mk", type="integer")
            yield Label(f"[ Source: {self.cfg.home} ]", id="status_bar", markup=False)
            with Horizontal(id="actions"):
                yield ActButton("[ B ] Start backup", id="btn_start", classes="act_btn")
                yield ActButton("[ Q ] Quit", id="btn_quit", classes="act_btn")
        yield Footer()

    def on_mount(self) -> None:
        if not self.candidates:
            self._set_status(
                "No destination configured. Set TARGET_DIR or REMOTE_HOST in the config."
            )
            return
        self.probe_worker()

    # -- sondeo de alcance (lento: ida y vuelta por SSH) ------------------------------
    @work(thread=True)
    def probe_worker(self) -> None:
        for i, d in enumerate(self.candidates):
            try:
                ok, why = d.reachable()
            except Exception as exc:  # noqa: BLE001
                ok, why = False, str(exc)
            self.call_from_thread(self._apply_probe, i, ok, why)
        self.call_from_thread(self._focus_first_destination)
        self.call_from_thread(self._set_status, "Tick the destination(s) you want, then press B.")

    def _focus_first_destination(self) -> None:
        # Mientras se sondea, los destinos están deshabilitados y el foco cae en el siguiente
        # widget (ruta/opciones). Al terminar, el cursor va al primer destino habilitado.
        if isinstance(self.focused, Input):  # no le robamos el foco a quien ya está tipeando
            return
        for i in range(len(self.candidates)):
            cb = self.query_one(f"#dest_{i}", Checkbox)
            if not cb.disabled:
                cb.focus()
                return

    def _apply_probe(self, i: int, ok: bool, why: str) -> None:
        self.reach[i] = (ok, why)
        d = self.candidates[i]
        cb = self.query_one(f"#dest_{i}", Checkbox)
        cb.disabled = not ok  # alcanzable => habilitado; JAMÁS se marca por el usuario
        mark = why if ok else f"unreachable - {why}"
        cb.label = f"{d.label}   {d.display_root()}   ({mark})"

    def _selected_route(self) -> str:
        if self._remote_idx is None:
            return "auto"
        pressed = self.query_one("#route", RadioSet).pressed_button
        return {"route_lan": "lan", "route_wan": "wan"}.get(pressed.id if pressed else "", "auto")

    def _set_status(self, msg: str) -> None:
        self.query_one("#status_bar", Label).update(f"[ {msg} ]")

    def _int_field(self, wid: str, fallback: int, lo: int = 0) -> int:
        try:
            return max(lo, int(self.query_one(wid, Input).value))
        except ValueError:
            return fallback

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_start":
            self.action_start()
        elif event.button.id == "btn_quit":
            self.action_quit()

    def action_start(self) -> None:
        chosen = [
            self.candidates[i]
            for i in range(len(self.candidates))
            if not self.query_one(f"#dest_{i}", Checkbox).disabled
            and self.query_one(f"#dest_{i}", Checkbox).value
        ]
        if not chosen:
            self._set_status("Nothing selected - tick at least one reachable destination.")
            return
        route = self._selected_route()
        if self._remote_idx is not None:
            self.candidates[self._remote_idx].force_route(route)
        self.result = {
            "destinations": chosen,
            "remote_route": route,
            "dry_run": self.query_one("#opt_dryrun", Checkbox).value,
            "max_disk_percent": self._int_field("#in_pct", self.cfg.max_disk_percent, lo=10),
            "remote_max_gb": self._int_field("#in_gb", self.cfg.remote_max_gb, lo=0),
            "min_keep": self._int_field("#in_mk", self.cfg.min_keep, lo=1),
        }
        self.exit(self.result)

    def action_quit(self) -> None:
        self.result = None
        self.exit(None)


def run_menu(cfg: Config, candidates: List) -> Optional[dict]:
    return BackupMenuApp(cfg, candidates).run()
