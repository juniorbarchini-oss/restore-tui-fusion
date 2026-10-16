"""Docs: enlaces relativos que resuelven, checklist completa y ejecutable, sin secretos ni IPs reales."""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = [REPO / "README.md", REPO / "docs/MIGRATION.md", REPO / "docs/HARDWARE_CHECKLIST.md"]
CHECKLIST = (REPO / "docs/HARDWARE_CHECKLIST.md").read_text()
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(doc):
    for target in LINK.findall(doc.read_text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert (doc.parent / target).exists(), f"{doc.name}: broken link {target}"


def test_every_plan_hardware_item_is_in_the_checklist_with_the_four_parts():
    plan = (REPO / "docs/PLAN_FUSION.md").read_text()
    plan_ids = set(re.findall(r"^\| (H\d+) \|", plan, re.M))
    assert plan_ids == {f"H{i}" for i in range(1, 17)}
    for h in sorted(plan_ids, key=lambda x: int(x[1:])):
        m = re.search(
            rf"^## {h}  .*?(?=^## H\d+  |^---\n\n## Result table)", CHECKLIST, re.M | re.S
        )
        assert m, f"{h} missing in the checklist"
        section = m.group(0)
        assert "**Where:**" in section, h
        assert "**Expect" in section or "**Run / Expect" in section, h
        assert "**Run" in section, h
        assert re.search(rf"^\| {h} \|", CHECKLIST, re.M), f"{h} missing in the result table"


def test_checklist_commands_use_real_flags():
    from restore_fusion import cli_backup

    flags = {a for act in cli_backup.build_parser()._actions for a in act.option_strings}
    for used in re.findall(r"backup-now[^\n`]*", CHECKLIST):
        for f in re.findall(r"(--[a-z][a-z-]+)", used):
            assert f in flags, f"checklist uses an unknown backup-now flag {f}"


def test_checklist_bash_blocks_are_syntactically_valid():
    import subprocess

    for block in re.findall(r"```bash\n(.*?)```", CHECKLIST, re.S):
        block = re.sub(r"<[^>\n]+>", "X", block)  # placeholders como <clone>, <USB>
        r = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True)
        assert r.returncode == 0, (block, r.stderr)


def test_pytest_command_in_the_checklist_names_an_existing_test_file():
    for path in re.findall(r"pytest (tests/[\w/.]+\.py)", CHECKLIST):
        assert (REPO / path).is_file()
    assert "RESTORE_FUSION_E2E=1" in CHECKLIST


def test_docs_do_not_contain_ips_or_secrets():
    for doc in DOCS:
        text = doc.read_text()
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text), doc.name
        assert not re.search(r"(?i)password\s*=\s*[^<\s]", text), doc.name


def test_migration_covers_both_editions_and_rollback():
    text = (REPO / "docs/MIGRATION.md").read_text()
    for needle in ("From `btrfs-restore-tui`", "From `restore-tui`", "Rolling back",
                   "/opt/restore-fusion-archive", "received_uuid", "No default host"):  # fmt: skip
        assert needle in text or needle.lower() in text.lower(), needle
