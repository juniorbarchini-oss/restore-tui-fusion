"""model/manifest.py: regla 2 (parcial/fallido nunca baseline ni latest) y clasificación v1/v2."""

import itertools

import pytest

from restore_fusion.model.manifest import (
    COMPLETED,
    FAILED,
    PARTIAL,
    RUNNING,
    SnapshotRecord,
    UNKNOWN,
    can_transition,
    classify_engine,
    history_of_other_engine,
    is_valid_snapshot_name,
    may_update_latest,
    new_manifest,
    parse_manifest,
    pick_baseline,
    status_of,
    transition,
    would_be_full,
)

STATUSES = [COMPLETED, PARTIAL, FAILED, RUNNING, "bogus", None]
ENGINES = ["rsync", "btrfs"]


def rec(name, status, engine, entries=()):
    m = {} if status is None else {"status": status}
    if engine:
        m["engine"] = engine
    return SnapshotRecord(name, m, tuple(entries))


# ---- baseline: tabla estado x motor ---------------------------------------


@pytest.mark.parametrize("status,engine", list(itertools.product(STATUSES, ENGINES)))
def test_only_completed_same_engine_is_a_baseline(status, engine):
    r = rec("2026-01-01_100000", status, engine)
    for asked in ENGINES:
        got = pick_baseline([r], asked)
        if status == COMPLETED and asked == engine:
            assert got is r
        else:
            assert got is None


@pytest.mark.parametrize("bad", [PARTIAL, FAILED, RUNNING, "bogus", None])
def test_newer_bad_snapshot_does_not_hide_older_completed(bad):
    good = rec("2026-01-01_100000", COMPLETED, "rsync")
    newer = rec("2026-01-02_100000", bad, "rsync")
    assert pick_baseline([good, newer], "rsync") is good


def test_newest_completed_wins_and_suffix_sorts_after():
    a = rec("2026-01-01_100000", COMPLETED, "rsync")
    b = rec("2026-01-01_100000_2", COMPLETED, "rsync")
    c = rec("2025-12-31_235959", COMPLETED, "rsync")
    assert pick_baseline([c, b, a], "rsync") is b


def test_invalid_names_are_never_baselines():
    for n in ("latest", "../2026-01-01_100000", "2026-01-01", "snapshots"):
        assert pick_baseline([rec(n, COMPLETED, "rsync")], "rsync") is None


def test_engine_mismatch_means_full_backup():
    recs = [rec("2026-01-01_100000", COMPLETED, "btrfs")]
    assert would_be_full(recs, "rsync") is True
    assert would_be_full(recs, "btrfs") is False
    assert history_of_other_engine(recs, "rsync") == ["2026-01-01_100000"]
    assert history_of_other_engine(recs, "btrfs") == []


def test_empty_history_is_full():
    assert would_be_full([], "rsync") and would_be_full([], "btrfs")


def test_unidentifiable_completed_snapshot_is_not_a_baseline():
    r = SnapshotRecord("2026-01-01_100000", {"status": COMPLETED})
    assert r.engine is None
    assert pick_baseline([r], "rsync") is None and pick_baseline([r], "btrfs") is None
    assert history_of_other_engine([r], "rsync") == []


# ---- latest ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [(COMPLETED, True), (PARTIAL, False), (FAILED, False), (RUNNING, False), (UNKNOWN, False)],
)
def test_latest_moves_only_on_completed(status, expected):
    assert may_update_latest(status) is expected


# ---- máquina de estados ---------------------------------------------------


def test_status_machine():
    assert can_transition(None, RUNNING)
    for final in (COMPLETED, PARTIAL, FAILED):
        assert can_transition(RUNNING, final)
    assert not can_transition(None, COMPLETED)
    for old in (COMPLETED, PARTIAL, FAILED):
        for new in (RUNNING, COMPLETED, PARTIAL, FAILED):
            assert not can_transition(old, new), (old, new)
    assert not can_transition(RUNNING, "bogus")
    assert not can_transition(RUNNING, RUNNING)


def test_partial_cannot_be_promoted_to_completed():
    m = new_manifest("rsync", "2026-01-01_100000", RUNNING)
    m = transition(m, PARTIAL, error="x")
    with pytest.raises(ValueError):
        transition(m, COMPLETED)
    assert m["status"] == PARTIAL


def test_transition_returns_copy_and_merges_fields():
    m = new_manifest("btrfs", "2026-01-01_100000", RUNNING)
    out = transition(m, COMPLETED, parents={"root": None})
    assert (
        m["status"] == RUNNING and out["status"] == COMPLETED and out["parents"] == {"root": None}
    )


def test_transition_from_empty_manifest_must_start_running():
    with pytest.raises(ValueError):
        transition({}, COMPLETED)
    assert transition({}, RUNNING)["status"] == RUNNING


# ---- clasificación v1 / v2 --------------------------------------------------


def test_v2_declares_engine():
    m = new_manifest("btrfs", "2026-01-01_100000", COMPLETED)
    assert m["manifest_version"] == 2 and classify_engine(m) == "btrfs"


def test_unknown_declared_engine_is_none():
    assert classify_engine({"engine": "zfs", "rsync_exit": 0}) is None


def test_v1_rsync_manifest_by_fields():
    # forma real del manifest de restore-tui
    m = {
        "manifest_version": 1,
        "status": "completed",
        "source_home": "/home/u",
        "link_dest_baseline": None,
        "rsync_exit": 0,
    }
    assert classify_engine(m) == "rsync"


def test_v1_btrfs_manifest_by_fields():
    # forma real del manifest de btrfs-restore-tui
    m = {
        "manifest_version": 1,
        "status": "completed",
        "target_is_btrfs": True,
        "local_snapshots": {"root": "root_x"},
        "parents": {"root": None},
    }
    assert classify_engine(m) == "btrfs"


def test_v1_running_manifest_without_distinct_fields_uses_layout():
    m = {"manifest_version": 1, "status": "running"}
    assert classify_engine(m, ["home", "manifest.json", "_system_state"]) == "rsync"
    assert classify_engine(m, ["root_20260101_100000", "home_20260101_100000"]) == "btrfs"
    assert classify_engine(m, ["root.btrfs.zst", "home.btrfs.zst"]) == "btrfs"
    assert classify_engine(m, ["manifest.json"]) is None


def test_contradictory_hints_are_none():
    assert classify_engine({"rsync_exit": 0, "parents": {}}) is None
    assert classify_engine({"rsync_exit": 0}, ["root_20260101_100000"]) is None


def test_v1_completed_rsync_is_a_baseline_for_rsync_only():
    r = SnapshotRecord("2026-01-01_100000", {"status": "completed", "rsync_exit": 0})
    assert pick_baseline([r], "rsync") is r and pick_baseline([r], "btrfs") is None


def test_v1_manifest_is_not_modified_by_classification():
    m = {"manifest_version": 1, "status": "completed", "rsync_exit": 0}
    before = dict(m)
    classify_engine(m)
    SnapshotRecord("2026-01-01_100000", m).engine
    assert m == before


# ---- parseo ---------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "not json", "[1,2]", "null", "3"])
def test_parse_manifest_never_raises(text):
    assert parse_manifest(text) == {}


def test_parse_manifest_ok_and_status_of():
    assert status_of(parse_manifest('{"status": "completed"}')) == COMPLETED
    assert status_of({}) == UNKNOWN and status_of({"status": "weird"}) == UNKNOWN


def test_snapshot_name_validation():
    assert is_valid_snapshot_name("2026-01-01_100000")
    assert is_valid_snapshot_name("2026-01-01_100000_2")
    assert not is_valid_snapshot_name("2026-01-01_10000")
    assert not is_valid_snapshot_name("")
    assert not is_valid_snapshot_name(None)


def test_new_manifest_rejects_bad_engine_or_status():
    with pytest.raises(ValueError):
        new_manifest("zfs", "n", RUNNING)
    with pytest.raises(ValueError):
        new_manifest("rsync", "n", "bogus")
