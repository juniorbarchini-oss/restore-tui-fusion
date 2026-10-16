"""Marcadores y salto automático de tests según las herramientas disponibles.

Un test que necesita una herramienta que falta se reporta SKIPPED (con motivo),
nunca como pasado.
"""

import importlib.util
import os
import shutil
import stat
import textwrap

import pytest

from restore_fusion.safety import ask_delete

_BINARIES = {
    "needs_rsync": "rsync",
    "needs_ssh": "ssh",
    "needs_btrfs": "btrfs",
}


def _has_textual() -> bool:
    return importlib.util.find_spec("textual") is not None


def _e2e_enabled() -> bool:
    return (
        os.environ.get("RESTORE_FUSION_E2E") == "1"
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
        and bool(shutil.which("mkfs.btrfs"))
        and bool(shutil.which("btrfs"))
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        for marker, binary in _BINARIES.items():
            if marker in item.keywords and not shutil.which(binary):
                item.add_marker(pytest.mark.skip(reason=f"{binary} not installed in this sandbox"))
        if "needs_textual" in item.keywords and not _has_textual():
            item.add_marker(pytest.mark.skip(reason="textual not installed"))
        if "needs_root_btrfs" in item.keywords and not _e2e_enabled():
            item.add_marker(
                pytest.mark.skip(
                    reason="needs root + mkfs.btrfs + RESTORE_FUSION_E2E=1 (real hardware)"
                )
            )
        # get_closest_marker, NOT `in item.keywords`: keywords also hold the folder names, so every
        # test under tests/hardware/ matched "hardware" and was skipped even with RESTORE_FUSION_E2E=1.
        if item.get_closest_marker("hardware") is not None:
            item.add_marker(pytest.mark.skip(reason="real-hardware test, run by hand"))


FAKE_SSH = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # ssh falso: ignora las opciones, toma user@host y ejecuta el resto con sh -c (igual que
    # el ssh real, que pasa la cadena al shell remoto). Sirve para probar el citado de comandos.
    echo "$@" >> "${FAKE_SSH_LOG:-/dev/null}"
    while [ $# -gt 0 ]; do
        case "$1" in
            -o|-p|-i|-l) shift 2 ;;
            -*) shift ;;
            *) break ;;
        esac
    done
    host="$1"; shift
    if [ "${FAKE_SSH_FAIL:-}" = 1 ]; then
        echo "ssh: Could not resolve hostname" >&2
        exit 255
    fi
    exec sh -c "$*"
    """).lstrip()


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """Pone un `ssh` falso al frente del PATH; devuelve la ruta del log de invocaciones."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "ssh"
    exe.write_text(FAKE_SSH)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.delenv("FAKE_SSH_FAIL", raising=False)
    return log


@pytest.fixture
def approve():
    """approve('a', 'b') -> Approval real (pasando por el prompt con respuesta 'y')."""

    def make(*names):
        return ask_delete(
            "delete?",
            list(names),
            input_fn=lambda _p: "y",
            is_tty=lambda: True,
            print_fn=lambda s: None,
        )

    return make
