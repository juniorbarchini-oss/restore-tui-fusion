"""Snap (solo en distros con apt)."""

from .base import CaptureContext


def capture_snaps(ctx: CaptureContext) -> None:
    if ctx.which("snap"):
        res = ctx.run(["snap", "list"])
        if res.returncode == 0:
            ctx.write("snap_list.txt", res.stdout)
