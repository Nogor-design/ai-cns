"""Reading a project's own documents: what it does, and where it says it goes."""

from __future__ import annotations

from pathlib import Path

from cortex import store, survey


def _write(repo: Path, name: str, text: str) -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_readme_supplies_the_description_and_badges_are_ignored(conn, project):
    repo = Path(project["repo_path"])
    _write(repo, "README.md", """# Widget Press

![build](https://img.shields.io/badge/build-passing-green)
[![coverage](https://x/y.svg)](https://x)

Widget Press turns spreadsheets into printable catalogues for small retailers.

## Install
Run `pip install widget-press`.
""")
    result = survey.build(conn, project)
    assert result.does.startswith("Widget Press turns spreadsheets")
    assert "img.shields.io" not in result.does


def test_the_current_phase_is_the_first_one_not_marked_done(conn, project):
    repo = Path(project["repo_path"])
    _write(repo, "docs/PLAN.md", """# Delivery Plan

## Phase 1 - Foundations (done)

Scope: the storage layer.

## Phase 2 - Sync engine

Scope: two-way sync with conflict reporting.
Exit: a 24-hour soak with no lost writes.

## Phase 3 - Billing

Scope: metered invoices.
""")
    result = survey.build(conn, project)
    assert result.plan_source == "docs/PLAN.md"
    assert result.plan_says == "Current phase: Phase 2 - Sync engine"
    assert any(step.startswith("Exit: a 24-hour soak") for step in result.next_steps)
    # Phase 3 has not started and Phase 1 is finished; neither is "next".
    assert not any("metered invoices" in step for step in result.next_steps)
    assert not any("storage layer" in step for step in result.next_steps)


def test_wrapped_bullets_survive_and_code_fences_are_not_read_as_plans(conn, project):
    repo = Path(project["repo_path"])
    _write(repo, "ROADMAP.md", """# Roadmap

Remaining before launch:

- Replace the import parser so it accepts the 2019 column order
  as well as the current one, without a second upload step.

```
- this is example output, not a plan
```
""")
    result = survey.build(conn, project)
    assert result.next_steps == (
        "Replace the import parser so it accepts the 2019 column order "
        "as well as the current one, without a second upload step.",
    )


def test_scaffold_placeholders_are_not_reported_as_work(conn, project):
    repo = Path(project["repo_path"])
    _write(repo, "TODO.md", """# To do

- [ ] <task> (type, priority)
- [ ] Add a CSV export
- [x] Ship the importer
""")
    result = survey.build(conn, project)
    assert result.next_steps == ("Add a CSV export",)


def test_next_steps_come_from_the_plan_not_from_abandoned_documents(conn, project):
    repo = Path(project["repo_path"])
    _write(repo, "docs/OLD-PROPOSAL.md", """# 2019 proposal

Remaining work:

- Port everything to CORBA.
""")
    plan = _write(repo, "docs/ROADMAP.md", """# Roadmap

Next up:

- Finish the billing screen.
""")
    # Make the roadmap unambiguously the newer document.
    import os
    os.utime(plan, (1_900_000_000, 1_900_000_000))
    result = survey.build(conn, project)
    assert result.next_steps == ("Finish the billing screen.",)
    assert not any("CORBA" in step for step in result.next_steps)


def test_generated_cortex_run_briefs_are_never_read_back_as_the_plan(conn, project):
    repo = Path(project["repo_path"])
    _write(repo, ".cortex/runs/abc123/brief.md", """# Brief

Next steps:

- Do whatever the last agent was told to do.
""")
    result = survey.build(conn, project)
    assert all("last agent" not in step for step in result.next_steps)
    assert all(not doc.path.startswith(".cortex/runs") for doc in result.documents)


def test_drift_reports_a_plan_that_has_not_moved_with_the_code(conn, project):
    repo = Path(project["repo_path"])
    stale = _write(repo, "docs/PLAN.md", "# Plan\n\nStatus: drafting.\n")
    import os
    os.utime(stale, (1_600_000_000, 1_600_000_000))  # 2020
    result = survey.build(conn, project)
    assert any("may be behind the work" in note for note in result.drift)


def test_an_unchanged_project_does_not_stack_identical_snapshots(conn, project):
    Path(project["repo_path"], "README.md").write_text(
        "# Demo\n\nA demo project.\n", encoding="utf-8"
    )
    first = survey.refresh(conn, project)
    assert first["changed"] is True
    second = survey.refresh(conn, project)
    assert second["changed"] is False
    assert second["survey"]["id"] == first["survey"]["id"]
    assert second["survey"]["checked_at"] >= first["survey"]["checked_at"]
    assert len(survey.history(conn, project["id"])) == 1

    # A real edit is a new snapshot, and the previous one is still there.
    Path(project["repo_path"], "README.md").write_text(
        "# Demo\n\nA demo project that now prints invoices.\n", encoding="utf-8"
    )
    third = survey.refresh(conn, project)
    assert third["changed"] is True
    assert len(survey.history(conn, project["id"])) == 2


def test_a_missing_repository_is_reported_rather_than_raising(conn):
    project_id = store.create_project(
        conn, name="Gone", repo_path="D:/does-not-exist-anywhere"
    )
    project = store.get_project(conn, project_id)
    result = survey.build(conn, project)
    assert result.documents == ()
    assert any("does not exist" in note for note in result.notes)


def test_removing_a_project_removes_its_surveys(conn, project):
    survey.refresh(conn, project)
    assert survey.latest(conn, project["id"]) is not None
    conn.execute("DELETE FROM project_surveys WHERE project_id = ?", (project["id"],))
    conn.commit()
    assert survey.latest(conn, project["id"]) is None


def test_registering_a_project_reads_it_immediately(conn, tmp_path):
    from cortex import project_registration

    repo = tmp_path / "fresh"
    repo.mkdir()
    (repo / "README.md").write_text(
        "# Fresh\n\nFresh keeps a register of warranty claims.\n", encoding="utf-8"
    )
    result = project_registration.register(conn, {
        "repo_path": str(repo), "name": "Fresh", "track_state": False,
    })
    stored = survey.latest(conn, result["project"]["id"])
    assert stored is not None
    assert stored["digest"]["does"].startswith("Fresh keeps a register")
