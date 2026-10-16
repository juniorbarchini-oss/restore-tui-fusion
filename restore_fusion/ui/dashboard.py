"""Panel en vivo de `backup-now` (rich). De btrfs-restore-tui/cli_backup.py (Dashboard)."""

import threading
import time
from collections import deque

from rich.panel import Panel
from rich.text import Text


class Dashboard:
    def __init__(self, title: str):
        self.title = title
        self.stage = "Initializing backup engine..."
        self.progress = ""
        self.logs: deque = deque(maxlen=5)
        self.start = time.time()
        self.lock = threading.Lock()

    def on_event(self, event_type: str, message: str) -> None:
        with self.lock:
            if event_type == "stage":
                self.stage = message
            elif event_type == "progress":
                self.progress = message
            else:
                tag = {"warning": "[yellow]!", "error": "[red]x", "success": "[green]+"}.get(
                    event_type, " "
                )
                line = f"{tag} {message}"
                if line not in self.logs:
                    self.logs.append(line)

    def render(self) -> Panel:
        with self.lock:
            mins, secs = divmod(int(time.time() - self.start), 60)
            body = Text()
            body.append(f" [{mins:02d}:{secs:02d}] ", style="bold yellow")
            body.append("Stage:  ", style="dim green")
            body.append(f"{self.stage}\n", style="bold green")
            if self.progress:
                body.append("           ", style="dim green")
                body.append(f"{self.progress}\n", style="bright_green")
            body.append("\n Activity:\n", style="dim white")
            for line in self.logs or ["  waiting..."]:
                body.append(f"  {line}\n", style="dim white")
            return Panel(
                body,
                title=f"[bold green]{self.title}[/bold green]",
                border_style="green",
                padding=(1, 2),
            )
