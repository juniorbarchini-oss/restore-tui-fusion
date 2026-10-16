"""Update check and self-update for an INSTALLED copy (/opt/restore-fusion).

`install.sh` records the installed commit in `<install dir>/VERSION`. The menu compares it with the
tip of `main` on GitHub (`git ls-remote`, no credentials, 3 s timeout, at most once a day, silent
when offline). Updating clones `main` into a temporary directory and runs its `install.sh
--upgrade`, which asks for sudo in the terminal as usual. Nothing is ever updated without a
confirmation. `CHECK_UPDATES=false` in config.conf turns the daily check off ([U] still works).
"""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

REPO_URL = "https://github.com/juniorbarchini-oss/restore-tui-fusion.git"
BRANCH = "main"
CHECK_EVERY = 24 * 3600  # seconds between automatic checks
GIT_TIMEOUT = 3
_PKG_ROOT = Path(__file__).resolve().parent.parent
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}


def is_installed(root: Optional[Path] = None) -> bool:
    """True for the copy made by install.sh (it leaves .owned-paths); false for a source tree."""
    return ((root or _PKG_ROOT) / ".owned-paths").exists()


def installed_commit(root: Optional[Path] = None) -> Optional[str]:
    try:
        v = ((root or _PKG_ROOT) / "VERSION").read_text().strip()
    except OSError:
        return None
    return v if v and v != "unknown" else None


def _git_env() -> dict:
    import os

    return {**os.environ, **_GIT_ENV}


def latest_commit(timeout: float = GIT_TIMEOUT) -> Optional[str]:
    """Tip of `main` on GitHub, or None when it cannot be reached (offline, no git, timeout)."""
    if shutil.which("git") is None:
        return None
    try:
        out = subprocess.run(
            ["git", "ls-remote", REPO_URL, f"refs/heads/{BRANCH}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = out.stdout.split()
    return parts[0] if out.returncode == 0 and parts and len(parts[0]) == 40 else None


def cache_file(home: Path) -> Path:
    return Path(home) / ".local" / "state" / "restore-tui" / "update-check"


def _read_cache(home: Path) -> Tuple[float, str]:
    try:
        ts, _, sha = cache_file(home).read_text().strip().partition(" ")
        return float(ts), sha
    except (OSError, ValueError):
        return 0.0, ""


def _write_cache(home: Path, sha: str, now: float) -> None:
    try:
        f = cache_file(home)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{int(now)} {sha}\n")
    except OSError:
        pass  # the cache is a convenience, never an error


def available(
    home: Path,
    enabled: bool = True,
    root: Optional[Path] = None,
    fetch: Callable[[], Optional[str]] = latest_commit,
    now: Optional[float] = None,
) -> Optional[str]:
    """Commit of a newer version, or None (up to date, unknown, disabled or offline)."""
    if not enabled or not is_installed(root):
        return None
    mine = installed_commit(root)
    if mine is None:
        return None
    now = time.time() if now is None else now
    ts, sha = _read_cache(home)
    if now - ts >= CHECK_EVERY:
        sha = fetch() or sha  # offline: keep the last known value, retry tomorrow
        _write_cache(home, sha, now)
    return sha if sha and sha != mine else None


def run_update(
    console,
    home: Path,
    root: Optional[Path] = None,
    fetch: Callable[[], Optional[str]] = latest_commit,
    run: Callable = subprocess.run,
) -> bool:
    """Interactive update. True when a new version was installed (the caller should exit)."""
    if not is_installed(root):
        console.print("[yellow]  This is not an installed copy; update it with git instead.[/]")
        return False
    if shutil.which("git") is None:
        console.print("[yellow]  git is required to update and was not found.[/]")
        return False
    latest = fetch()
    if latest is None:
        console.print("[yellow]  Could not reach GitHub; try again later.[/]")
        return False
    mine = installed_commit(root)
    if mine == latest:
        console.print(f"[#00FF66]  Already up to date ({latest[:7]}).[/]")
        return False
    was = mine[:7] if mine else "unknown version"
    answer = (
        console.input(f"[bold #00FF66]  Install {latest[:7]} (now {was})? Needs sudo. [y/N] [/]")
        .strip()
        .lower()
    )
    if answer not in ("y", "yes"):
        console.print("  Cancelled.")
        return False
    tmp = Path(tempfile.mkdtemp(prefix="restore-fusion-update-"))
    try:
        src = tmp / "src"
        cl = run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", BRANCH, REPO_URL, str(src)],
            env=_git_env(),
        )
        if cl.returncode != 0:
            console.print("[red]  Download failed; nothing was changed.[/]")
            return False
        inst = run(["./install.sh", "--upgrade", "--yes"], cwd=src)
        if inst.returncode != 0:
            console.print("[red]  The installer failed; see its output above.[/]")
            return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _write_cache(home, latest, time.time())
    console.print("[bold #00FF66]  Updated. Start restore-tui again to use the new version.[/]")
    return True
