"""PBS: cliente (listar/montar/desmontar), servicios de restauración y la app compartida.
Cliente simulado; el montaje FUSE y el servidor reales NO se prueban aquí (hardware)."""

import asyncio
import io

import pytest
from pbs_fakes import SECRET, T1, T2, FakePbs
from rich.console import Console

from restore_fusion.pbs import restore as pbs_restore
from restore_fusion.pbs.client import PbsClient, PbsError
from restore_fusion.pbs.mount import PbsServices, split_area_path

noop = lambda _s: None  # noqa: E731


def client(cfg, fake, **kw):
    return PbsClient(cfg, run=fake, which=lambda t: "/bin/" + t, sleep=noop, **kw)


# -- listar ------------------------------------------------------------------------------------------------
def test_list_snapshots_newest_first_and_argv(cfg, fake):
    fake.snapshots = [(T1, 1), (T2, 2)]
    snaps = client(cfg, fake).list_snapshots("box")
    assert [s.backup_time for s in snaps] == [T2, T1]
    argv, env = fake.calls[0]
    assert argv[:4] == ["proxmox-backup-client", "snapshot", "list", "host/box"]
    assert "--repository" in argv and SECRET not in " ".join(argv)
    assert env["PBS_PASSWORD"] == SECRET


def test_failed_listing_is_an_error_not_empty(cfg, fake):
    fake.fail["snapshot list"] = (1, "permission denied")
    with pytest.raises(PbsError, match="permission denied"):
        client(cfg, fake).list_snapshots("box")


def test_garbage_listing_is_an_error(cfg, fake):
    def bad(argv, **kw):
        import subprocess

        return subprocess.CompletedProcess(argv, 0, "not json", "")

    with pytest.raises(PbsError):
        PbsClient(cfg, run=bad).list_snapshots("box")


def test_snapshot_path_format(cfg, fake):
    s = client(cfg, fake).list_snapshots("box")[0]
    assert s.path.startswith("host/box/") and s.path.endswith("Z") and "T" in s.path


def test_archives_parsed_and_fallback(cfg, fake):
    c = client(cfg, fake)
    assert c.archives("host/box/x") == ["root.pxar", "home.pxar"]
    fake.fail["snapshot files"] = (1, "nope")
    assert c.archives("host/box/x") == ["root.pxar", "home.pxar"]
    fake.fail.clear()
    fake.archives = ["home.pxar"]
    assert c.archives("host/box/x") == ["home.pxar"]


# -- montar / desmontar -------------------------------------------------------------------------------------------
def test_mount_argv_and_wait(cfg, fake, tmp_path):
    fake.mount_content["home.pxar"] = {"a.txt": "x"}
    target = tmp_path / "m"
    client(cfg, fake).mount("host/box/z", "home.pxar", target)
    argv = fake.tool_calls()[-1]
    assert argv == ["proxmox-backup-client", "mount", "host/box/z", "home.pxar", str(target),
                    "--repository", cfg.pbs_repository]  # fmt: skip


def test_mount_failure_and_timeout_raise(cfg, fake, tmp_path):
    fake.fail["mount"] = (1, "fuse missing")
    with pytest.raises(PbsError, match="fuse missing"):
        client(cfg, fake).mount("s", "home.pxar", tmp_path / "a")
    fake.fail.clear()  # monta pero nunca aparece nada
    with pytest.raises(PbsError, match="nothing appeared"):
        client(cfg, fake, wait_seconds=0.3).mount("s", "home.pxar", tmp_path / "b")


def test_unmount_uses_rmdir_never_deletes_contents(cfg, fake, tmp_path):
    t = tmp_path / "still-mounted"
    t.mkdir()
    (t / "precious.txt").write_text("keep")  # simula un montaje que no se pudo soltar
    client(cfg, fake).unmount(t)
    assert (t / "precious.txt").read_text() == "keep"
    empty = tmp_path / "empty"
    empty.mkdir()
    client(cfg, fake).unmount(empty)
    assert not empty.exists()


# -- elegir snapshot -----------------------------------------------------------------------------------------------
def snaps(cfg, fake):
    return client(cfg, fake).list_snapshots("box")


def quiet():
    return Console(file=io.StringIO(), width=200)


@pytest.mark.parametrize("answer", ["", "  ", "abc", "9", "-1"])
def test_pick_has_no_default(cfg, fake, answer):
    """Enter vacío ya no significa 'el más nuevo': no se elige nada por el usuario."""
    assert pbs_restore.pick_snapshot(snaps(cfg, fake), lambda _p: answer, quiet()) is None


def test_pick_by_number_and_eof(cfg, fake):
    s = snaps(cfg, fake)
    assert pbs_restore.pick_snapshot(s, lambda _p: "1", quiet()) == s[1]

    def eof(_p):
        raise EOFError

    assert pbs_restore.pick_snapshot(s, eof, quiet()) is None
    assert pbs_restore.pick_snapshot([], lambda _p: "0", quiet()) is None


# -- servicios --------------------------------------------------------------------------------------------------------
def services(cfg, fake, tmp_path, **kw):
    return PbsServices(cfg, client(cfg, fake), "box", base=tmp_path / "run", **kw)


def test_snapshots_have_root_and_home_areas_for_btrfs_pushes(cfg, fake, tmp_path):
    svc = services(cfg, fake, tmp_path)
    snap = svc.local_snapshots()[0]
    (origin,) = snap.origins
    assert origin.label == "PBS" and [a.kind for a in origin.areas] == ["root", "home"]
    assert origin.area("root").target_base.as_posix() == "/"
    assert origin.area("home").target_base == cfg.home
    assert origin.area("home").home_user_subdir is True


def test_rsync_pushes_only_have_home_and_no_user_subdir(cfg, fake, tmp_path):
    fake.archives = ["home.pxar"]
    origin = services(cfg, fake, tmp_path).local_snapshots()[0].origins[0]
    assert [a.kind for a in origin.areas] == ["home"]
    assert origin.areas[0].home_user_subdir is False


def test_listing_failure_propagates(cfg, fake, tmp_path):
    fake.fail["snapshot list"] = (1, "unreachable")
    with pytest.raises(PbsError):
        services(cfg, fake, tmp_path).local_snapshots()


def test_prepare_mounts_once_and_enters_user_dir(cfg, fake, tmp_path):
    fake.mount_content["home.pxar"] = {"tester/doc.txt": "d"}
    svc = services(cfg, fake, tmp_path)
    origin = svc.local_snapshots()[0].origins[0]
    area = origin.area("home")
    root = svc.prepare(origin, area)
    assert root.name == "tester" and (root / "doc.txt").read_text() == "d"
    assert svc.prepare(origin, area) == root
    assert len([a for a in fake.tool_calls() if a[1] == "mount"]) == 1


def test_prepare_failure_leaves_nothing_mounted_state(cfg, fake, tmp_path):
    fake.fail["mount"] = (1, "boom")
    svc = services(cfg, fake, tmp_path)
    origin = svc.local_snapshots()[0].origins[0]
    with pytest.raises(PbsError):
        svc.prepare(origin, origin.area("home"))
    assert svc._mounted == {}


def test_cleanup_unmounts_and_removes_only_empty_dirs(cfg, fake, tmp_path):
    fake.mount_content["root.pxar"] = {"etc/hosts": "h"}
    svc = services(cfg, fake, tmp_path)
    origin = svc.local_snapshots()[0].origins[0]
    mnt = svc.prepare(origin, origin.area("root"))
    svc.cleanup()
    tools = [a[0] for a, _ in fake.calls if a[0] != "proxmox-backup-client"]
    assert tools and tools[0] in ("/bin/fusermount3", "/bin/fusermount")
    # el contenido (que en la realidad ya no existe tras desmontar) nunca se borra con rmtree
    assert (mnt / "etc" / "hosts").exists()


def test_only_restricts_to_the_chosen_snapshot(cfg, fake, tmp_path):
    chosen = snaps(cfg, fake)[1]
    out = services(cfg, fake, tmp_path, only=chosen).local_snapshots()
    assert len(out) == 1 and out[0].timestamp == chosen.local_time


def test_split_area_path():
    assert split_area_path("host/box/2026-01-01T00:00:00Z/root.pxar") == (
        "host/box/2026-01-01T00:00:00Z",
        "root.pxar",
    )


# -- run_restore -------------------------------------------------------------------------------------------------------
def test_run_restore_guards(cfg, fake, tmp_path):
    cfg.pbs_password = ""
    assert pbs_restore.run_restore(cfg, client=client(cfg, fake)) == 1
    cfg.pbs_password = SECRET
    nobin = PbsClient(cfg, run=fake, which=lambda t: None)
    assert pbs_restore.run_restore(cfg, client=nobin) == 1
    fake.fail["snapshot list"] = (1, "x")
    assert pbs_restore.run_restore(cfg, client=client(cfg, fake)) == 1


def test_run_restore_without_a_pick_mounts_nothing(cfg, fake, tmp_path):
    rc = pbs_restore.run_restore(
        cfg, client=client(cfg, fake), ask=lambda _p: "", base=tmp_path / "r"
    )
    assert rc == 1 and not [a for a in fake.tool_calls() if a[1] == "mount"]


def test_run_restore_cleans_up_even_if_the_app_crashes(cfg, fake, tmp_path):
    seen = {}

    def factory(svc):
        seen["svc"] = svc

        class Boom:
            def run(self):
                raise RuntimeError("ui crashed")

        return Boom()

    with pytest.raises(RuntimeError):
        pbs_restore.run_restore(
            cfg,
            client=client(cfg, fake),
            ask=lambda _p: "0",
            app_factory=factory,
            base=tmp_path / "r",
        )
    assert seen["svc"]._mounted == {}


# -- la app compartida sobre PbsServices -------------------------------------------------------------------------------
@pytest.mark.needs_textual
def test_shared_restore_app_browses_pbs_and_restores_with_bak(cfg, fake, tmp_path):
    from restore_fusion.model.restore import ConflictResolution
    from restore_fusion.restore.engine import RestoreEngine
    from restore_fusion.ui.restore_app import RestoreApp

    fake.archives = ["home.pxar"]
    fake.mount_content["home.pxar"] = {"docs/a.txt": "from pbs", "b.txt": "b"}
    target = tmp_path / "live"
    (target / "docs").mkdir(parents=True)
    (target / "docs" / "a.txt").write_text("current")
    cfg.home = target
    svc = services(cfg, fake, tmp_path)

    async def go():
        eng = RestoreEngine(user="x", home=target, target_uid=0, target_gid=0)
        app = RestoreApp(svc, eng, user_home=target)
        async with app.run_test(size=(140, 40)) as pilot:
            for _ in range(30):
                await pilot.pause(0.1)
            assert len(app.snapshots) == 2 and app.selected == set()
            app.select_snapshot(app.snapshots[0].key)
            for _ in range(30):
                await pilot.pause(0.1)
            assert {e["name"] for e in app.entries} == {"docs", "b.txt"}
            assert app.selected == set()
            app.selected = {"docs"}
            items = app._items()
            list(eng.restore_generator(items, target, ConflictResolution.BACKUP))

    asyncio.run(go())
    assert (target / "docs" / "a.txt").read_text() == "from pbs"
    assert (target / "docs" / "a.txt.bak").read_text() == "current"
