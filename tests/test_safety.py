"""safety/: regla 4 (nunca borrar solo, --yes no aprueba borrados, default No, sin tty = No)
y plan de retención (decisión 1)."""

import inspect
import sys

import pytest

from restore_fusion.model.manifest import SnapshotRecord
from restore_fusion.safety import (
    Approval,
    ApprovalError,
    PrunePlan,
    RetentionPolicy,
    apply_deletions,
    approve_retention,
    ask_delete,
    confirm,
    confirm_action,
    plan_prune,
)
from restore_fusion.safety import approval as approval_mod


def tty():
    return True


def notty():
    return False


def answers(*vals):
    it = iter(vals)
    calls = []

    def fn(prompt):
        calls.append(prompt)
        v = next(it)
        if isinstance(v, BaseException):
            raise v
        return v

    fn.calls = calls
    return fn


def must_not_prompt(_p):
    raise AssertionError("prompted when it must not have")


# ---- confirm: default No ---------------------------------------------------


@pytest.mark.parametrize(
    "ans,expected",
    [
        ("", False),
        ("n", False),
        ("N", False),
        ("no", False),
        ("  ", False),
        ("maybe", False),
        ("yep", False),
        ("yy", False),
        ("y", True),
        ("Y", True),
        ("yes", True),
        (" YES ", True),
    ],
)
def test_confirm_only_y_or_yes_is_yes(ans, expected):
    assert confirm("ok?", input_fn=answers(ans), is_tty=tty) is expected


def test_prompt_shows_default_no():
    fn = answers("")
    confirm("Delete it?", input_fn=fn, is_tty=tty)
    assert fn.calls == ["Delete it? [y/N] "]


@pytest.mark.parametrize("exc", [EOFError(), KeyboardInterrupt()])
def test_eof_and_ctrl_c_mean_no(exc):
    assert confirm("ok?", input_fn=answers(exc), is_tty=tty) is False


def test_no_tty_means_no_and_does_not_prompt():
    assert confirm("ok?", input_fn=must_not_prompt, is_tty=notty) is False


def test_default_tty_check_is_false_under_pytest():
    # pytest captura stdin: no es una terminal
    assert sys.modules["restore_fusion.safety.confirm"]._stdin_is_tty() is False


# ---- --yes: aprueba acciones no destructivas, jamás borrados -----------------


def test_yes_approves_a_full_backup_without_prompting():
    assert confirm_action("full?", assume_yes=True, input_fn=must_not_prompt, is_tty=notty)


def test_full_backup_without_yes_and_without_tty_is_no():
    assert confirm_action("full?", input_fn=must_not_prompt, is_tty=notty) is False


def test_ask_delete_has_no_yes_parameter():
    params = inspect.signature(ask_delete).parameters
    assert not [p for p in params if "yes" in p.lower() or "force" in p.lower() or "assume" in p]


def test_yes_flag_cannot_reach_a_deletion():
    # camino típico del CLI: el flag --yes solo existe para confirm_action
    yes_flag = True
    assert confirm_action("full backup?", assume_yes=yes_flag, is_tty=notty)
    # el borrado en la misma corrida, con --yes puesto, sigue sin aprobación
    assert (
        ask_delete("delete?", ["2026-01-01_100000"], input_fn=must_not_prompt, is_tty=notty) is None
    )


def test_ask_delete_no_tty_is_none_and_never_prompts():
    assert ask_delete("d?", ["a"], input_fn=must_not_prompt, is_tty=notty) is None


def test_ask_delete_default_is_no():
    out = []
    assert (
        ask_delete("d?", ["a", "b"], input_fn=answers(""), is_tty=tty, print_fn=out.append) is None
    )
    assert out == ["  a", "  b"]  # lista lo que se borraría antes de preguntar


def test_ask_delete_nothing_to_delete_is_none():
    assert ask_delete("d?", [], input_fn=must_not_prompt, is_tty=tty) is None


def test_ask_delete_yes_returns_approval_for_exactly_those_items():
    ap = ask_delete("d?", ["a", "b"], input_fn=answers("y"), is_tty=tty, print_fn=lambda s: None)
    assert isinstance(ap, Approval) and ap.origin == "user-prompt"
    assert ap.covers("delete", "a") and ap.covers("delete", "b")
    assert not ap.covers("delete", "c") and not ap.covers("other", "a")


# ---- Approval no se fabrica -----------------------------------------------------


def test_approval_cannot_be_built_directly():
    with pytest.raises(TypeError):
        Approval()
    with pytest.raises(TypeError):
        Approval("delete", ["a"], "user-prompt")
    with pytest.raises(TypeError):
        Approval._mint(object(), "delete", ["a"], "user-prompt")


def test_approval_is_immutable():
    ap = ask_delete("d?", ["a"], input_fn=answers("y"), is_tty=tty, print_fn=lambda s: None)
    with pytest.raises(AttributeError):
        ap._items = frozenset({"x"})
    with pytest.raises(AttributeError):
        ap.extra = 1


def test_unknown_origin_rejected():
    with pytest.raises(ValueError):
        Approval._mint(approval_mod._MINT_KEY, "delete", ["a"], "from-a-flag")


# ---- apply_deletions -----------------------------------------------------------


def _ap(items):
    return ask_delete("d?", items, input_fn=answers("y"), is_tty=tty, print_fn=lambda s: None)


def test_delete_without_approval_is_refused():
    called = []
    with pytest.raises(ApprovalError):
        apply_deletions(["a"], called.append, None)
    assert called == []


def test_delete_outside_approval_deletes_nothing_at_all():
    called = []
    with pytest.raises(ApprovalError, match="b"):
        apply_deletions(["a", "b"], called.append, _ap(["a"]))
    assert called == []


def test_delete_wrong_action_is_refused():
    with pytest.raises(ApprovalError):
        apply_deletions(["a"], lambda n: None, _ap(["a"]), action="wipe")


def test_delete_approved_items_and_report_failures():
    deleted = []

    def fn(n):
        if n == "b":
            raise OSError("busy")
        deleted.append(n)

    rep = apply_deletions(["a", "b", "c"], fn, _ap(["a", "b", "c"]))
    assert rep.deleted == ["a", "c"] and rep.failed == {"b": "busy"} and deleted == ["a", "c"]


# ---- plan_prune ---------------------------------------------------------------------


def R(name, status, engine="rsync"):
    return SnapshotRecord(name, {"status": status, "engine": engine})


def names(prefix_day, n, status="completed", engine="rsync"):
    return [R(f"2026-01-{prefix_day + i:02d}_100000", status, engine) for i in range(n)]


POL = RetentionPolicy(min_keep=2)


def test_plan_deletes_nothing_by_itself():
    plan = plan_prune(names(1, 5), POL, "rsync")
    assert isinstance(plan, PrunePlan)  # puro: es una lista de nombres, no borra


def test_never_below_min_keep_and_never_the_last():
    recs = names(1, 5)
    plan = plan_prune(recs, POL, "rsync")
    assert plan.budget_limit == 3
    assert plan.budget_candidates() == [r.name for r in recs[:3]]  # los más viejos primero
    assert recs[-1].name not in plan.all_names and recs[-2].name not in plan.all_names


@pytest.mark.parametrize("mk", [0, 1, -3])
def test_floor_is_at_least_one(mk):
    recs = names(1, 3)
    plan = plan_prune(recs, RetentionPolicy(min_keep=mk), "rsync")
    assert plan.keep_floor == 1 and plan.budget_limit == 2
    assert recs[-1].name not in plan.all_names


def test_single_completed_is_never_listed():
    assert plan_prune(names(1, 1), POL, "rsync").all_names == []


def test_fewer_than_floor_lists_nothing():
    assert plan_prune(names(1, 2), RetentionPolicy(min_keep=5), "rsync").all_names == []


def test_stale_only_older_than_newest_completed():
    recs = [
        R("2026-01-01_100000", "partial"),
        R("2026-01-02_100000", "failed"),
        R("2026-01-03_100000", "running"),
        R("2026-01-04_100000", "completed"),
        R("2026-01-05_100000", "partial"),  # más nuevo que el completed: se conserva
    ]
    plan = plan_prune(recs, POL, "rsync")
    assert plan.stale == ["2026-01-01_100000", "2026-01-02_100000", "2026-01-03_100000"]


def test_no_completed_means_no_stale_deleted():
    plan = plan_prune([R("2026-01-01_100000", "partial")], POL, "rsync")
    assert plan.all_names == []


def test_unknown_status_is_never_listed():
    recs = [R("2026-01-01_100000", "weird"), *names(2, 3)]
    assert "2026-01-01_100000" not in plan_prune(recs, POL, "rsync").all_names


def test_max_snapshots_cap_respects_floor():
    recs = names(1, 6)
    plan = plan_prune(recs, RetentionPolicy(min_keep=2, max_snapshots=4), "rsync")
    assert plan.over_cap == [r.name for r in recs[:2]]
    # un tope menor que MIN_KEEP no manda: se queda el piso
    plan = plan_prune(recs, RetentionPolicy(min_keep=3, max_snapshots=1), "rsync")
    assert len(recs) - len(plan.over_cap) == 3 and recs[-1].name not in plan.over_cap


def test_budget_candidates_exclude_cap_victims():
    recs = names(1, 6)
    plan = plan_prune(recs, RetentionPolicy(min_keep=2, max_snapshots=4), "rsync")
    assert not set(plan.over_cap) & set(plan.budget_candidates())
    assert plan.budget_limit == 2


def test_other_engine_and_unidentifiable_history_is_never_in_a_plan():
    recs = names(1, 4, engine="btrfs") + [
        SnapshotRecord("2026-01-09_100000", {"status": "completed"})
    ]
    assert plan_prune(recs, POL, "rsync").all_names == []
    stale_other = [
        R("2026-01-01_100000", "partial", "btrfs"),
        R("2026-01-05_100000", "completed", "rsync"),
    ]
    assert plan_prune(stale_other, POL, "rsync").all_names == []


def test_invalid_names_are_never_listed():
    recs = [R("latest", "partial"), R("../x", "completed"), *names(1, 4)]
    out = plan_prune(recs, POL, "rsync").all_names
    assert "latest" not in out and "../x" not in out


def test_dry_run_description():
    recs = [R("2026-01-01_100000", "partial"), *names(2, 5)]
    text = "\n".join(plan_prune(recs, POL, "rsync").describe())
    assert "2026-01-01_100000" in text and "at least 2" in text and "oldest first" in text
    assert plan_prune([], POL, "rsync").describe()[-1] == "nothing to prune"


# ---- retención configurada + Approval --------------------------------------------------


def test_retention_approval_covers_only_the_plan():
    recs = names(1, 5)
    plan = plan_prune(recs, POL, "rsync")
    ap = approve_retention(plan)
    assert ap.origin == "retention-config"
    for n in plan.all_names:
        assert ap.covers("delete", n)
    assert not ap.covers("delete", recs[-1].name)  # el último nunca
    with pytest.raises(ApprovalError):
        apply_deletions([recs[-1].name], lambda n: None, ap)


def test_retention_disabled_gives_no_approval():
    with pytest.raises(PermissionError):
        approve_retention(plan_prune(names(1, 5), POL, "rsync"), retention_enabled=False)


def test_approve_retention_has_no_yes_parameter():
    params = inspect.signature(approve_retention).parameters
    assert not [p for p in params if "yes" in p.lower() or "assume" in p]


def test_plan_then_delete_end_to_end():
    recs = [R("2026-01-01_100000", "partial"), *names(2, 4)]
    plan = plan_prune(recs, POL, "rsync")
    deleted = []
    apply_deletions(plan.all_names, deleted.append, approve_retention(plan))
    kept = {r.name for r in recs} - set(deleted)
    assert "2026-01-05_100000" in kept and len(kept) >= 2
