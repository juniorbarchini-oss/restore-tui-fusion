"""Doble de `proxmox-backup-client`: un `run` inyectable que registra argv y entorno."""

import json
import subprocess
import time
from pathlib import Path

SECRET = "s3cr3t-token-value"
T1 = int(time.mktime((2026, 9, 10, 10, 0, 0, 0, 0, -1)))
T2 = T1 + 86400


class FakePbs:
    """Callable con la firma de subprocess.run. `files` = nombres por snapshot; `on_mount` crea
    contenido en el punto de montaje (lo que haría FUSE)."""

    def __init__(
        self, snapshots=((T2, 3 * 1024**3), (T1, 1024**3)), archives=("root.pxar", "home.pxar")
    ):
        self.calls = []
        self.snapshots = list(snapshots)
        self.archives = list(archives)
        self.fail = {}  # subcomando -> (rc, stderr)
        self.mount_content = {}  # archive -> {rel: texto}
        self.mounted = []

    def __call__(self, argv, env=None, capture_output=False, text=False, **kw):
        self.calls.append((list(argv), env))
        if argv[0] != "proxmox-backup-client":
            self.mounted = [m for m in self.mounted if m != argv[-1]]
            return subprocess.CompletedProcess(argv, 0, "", "")
        sub = argv[1:3] if argv[1] == "snapshot" else argv[1:2]
        key = " ".join(sub)
        if key in self.fail:
            rc, err = self.fail[key]
            return subprocess.CompletedProcess(argv, rc, "", err)
        if key == "snapshot list":
            rows = [{"backup-id": "box", "backup-time": t, "size": s} for t, s in self.snapshots]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        if key == "snapshot files":
            rows = [{"filename": f"{a}.didx"} for a in self.archives] + [
                {"filename": "index.json.blob"}
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        if key == "mount":
            archive, target = argv[3], Path(argv[4])
            for rel, text in self.mount_content.get(archive, {}).items():
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                (target / rel).write_text(text)
            self.mounted.append(str(target))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def tool_calls(self):
        return [a for a, _ in self.calls if a[0] == "proxmox-backup-client"]
