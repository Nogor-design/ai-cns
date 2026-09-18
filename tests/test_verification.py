"""The Phase 3 gate: what merges, what is refused, and what the owner decides."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex import gitutil, integration, inbox, review, store, verification, worktrees


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def gated(conn, git_repo, tmp_path, monkeypatch):
    """A project, a write task, and its worktree branched from integration."""
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    project_id = store.create_project(
        conn, name="Gated", repo_path=str(git_repo), stack="Python",
        current_goal="ship", test_command="python -c \"pass\"",
    )
    project = store.get_project(conn, project_id)
    task_id = store.create_task(
        conn, project_id=project_id, title="Add a feature", type="code",
        acceptance="feature.py exists and says value = 1",
    )
    workspace = integration.ensure(git_repo, project_id)
    tree = worktrees.ensure(git_repo, project_id, task_id, base=integration.BRANCH)
    return {
        "conn": conn, "repo": git_repo, "project": project,
        "task": store.get_task(conn, task_id), "tree": tree,
        "integration": workspace, "before": gitutil.head(tree.path),
    }


def _approve(*args, **kwargs):
    return review.ReviewOutcome("pass", "codex", "default", ("looks right",))


def _refuse(*args, **kwargs):
    return review.ReviewOutcome("fail", "codex", "default", ("misses the criterion",))


def _tests(passing: bool):
    return lambda workspace, command: 1 if passing else 0


def test_clean_change_merges_into_integration(gated):
    (gated["tree"].path / "feature.py").write_text("value = 1\n", encoding="utf-8")

    report = verification.verify(
        gated["conn"], gated["task"], project=gated["project"],
        workspace=gated["tree"].path, before=gated["before"], producer="claude",
        review_fn=_approve, test_fn=_tests(True),
    )

    assert report.status == "merged"
    assert report.merge_commit
    assert (gated["integration"].path / "feature.py").read_text() == "value = 1\n"
    # Every check is recorded, not just the failing ones, so the owner can see
    # what was actually verified.
    names = [check.name for check in report.checks]
    assert names == [
        "changes", "path_scope", "protected_files", "secrets",
        "task_tests", "merge", "merged_tests", "review",
    ]
    stored = verification.get(gated["conn"], report.verification_id)
    assert stored["status"] == "merged"
    assert stored["merge_commit"] == report.merge_commit


def test_uncommitted_agent_edits_are_still_gated(gated):
    """Agents that edit without committing must not bypass the gate."""
    (gated["tree"].path / "feature.py").write_text("value = 1\n", encoding="utf-8")

    report = verification.verify(
        gated["conn"], gated["task"], project=gated["project"],
        workspace=gated["tree"].path, before=gated["before"], producer="claude",
        review_fn=_approve, test_fn=_tests(True),
    )

    changes = next(check for check in report.checks if check.name == "changes")
    assert changes.evidence["committed_by_cortex"] is True
    assert report.status == "merged"


def test_a_run_that_changed_nothing_is_rejected(gated):
    report = verification.verify(
        gated["conn"], gated["task"], project=gated["project"],
        workspace=gated["tree"].path, before=gated["before"], producer="claude",
        review_fn=_approve, test_fn=_tests(True),
    )

    assert report.status == "rejected"
    assert report.merge_commit is None
    assert [check.name for check in report.blockers()] == ["changes"]


def test_out_of_scope_change_is_rejected_before_any_merge(gated, conn):
    store.update_task(conn, gated["task"]["id"], allowed_paths='["docs/*"]')
    task = store.get_task(conn, gated["task"]["id"])
    (gated["tree"].path / "feature.py").write_text("value = 1\n", encoding="utf-8")
    head_before = gitutil.head(gated["integration"].path)

    report = verification.verify(
        conn, task, project=gated["project"], workspace=gated["tree"].path,
        before=gated["before"], producer="claude", review_fn=_approve, test_fn=_tests(True),
    )

    assert report.status == "rejected"
    scope = next(check for check in report.checks if check.name == "path_scope")
    assert scope.evidence["violations"] == ["feature.py"]
    assert gitutil.head(gated["integration"].path) == head_before
    # The merge step never ran, so no tokens were spent on a review either.
    assert "review" not in [check.name for check in report.checks]


def test_protected_file_becomes_an_owner_decision(gated, conn):
    workflows = gated["tree"].path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("on: push\n", encoding="utf-8")

    report = verification.verify(
        conn, gated["task"], project=gated["project"], workspace=gated["tree"].path,
        before=gated["before"], producer="claude", review_fn=_approve, test_fn=_tests(True),
    )

    assert report.status == "needs_owner"
    protected = next(check for check in report.checks if check.name == "protected_files")
    assert protected.status == verification.OWNER
    assert protected.evidence["protected"][0]["path"] == ".github/workflows/ci.yml"
    items = inbox.items(conn)
    assert items and items[0]["kind"] == "verification_decision"


def test_failing_merged_tests_rewind_the_integration_branch(gated, conn):
    (gated["tree"].path / "feature.py").write_text("value = 1\n", encoding="utf-8")
    head_before = gitutil.head(gated["integration"].path)

    report = verification.verify(
        conn, gated["task"], project=gated["project"], workspace=gated["tree"].path,
        before=gated["before"], producer="claude", review_fn=_approve,
        # Passes alone on its branch, fails once merged: exactly the case the
        # merged-result test exists to catch.
        test_fn=lambda workspace, command: 0 if "_integration" in str(workspace) else 1,
    )

    assert report.status == "rejected"
    assert report.merge_commit is None
    assert gitutil.head(gated["integration"].path) == head_before
    assert not (gated["integration"].path / "feature.py").exists()
    # A failed merged-result test makes the paid review pointless.
    assert next(c for c in report.checks if c.name == "review").status == verification.SKIPPED


def test_a_failed_review_rewinds_the_merge(gated, conn):
    (gated["tree"].path / "feature.py").write_text("value = 1\n", encoding="utf-8")
    head_before = gitutil.head(gated["integration"].path)

    report = verification.verify(
        conn, gated["task"], project=gated["project"], workspace=gated["tree"].path,
        before=gated["before"], producer="claude", review_fn=_refuse, test_fn=_tests(True),
    )

    assert report.status == "rejected"
    assert gitutil.head(gated["integration"].path) == head_before
    assert "misses the criterion" in report.summary()
    assert any(check.name == "rollback" for check in report.checks)


def test_a_conflict_goes_to_the_owner_and_is_never_resolved(gated, conn):
    # Something else already changed the same file on the integration branch.
    other = gated["repo"].parent / "other"
    _git(gated["repo"], "worktree", "add", "-b", "cortex/other", str(other), integration.BRANCH)
    (other / "shared.py").write_text("theirs = 1\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=a@b.c", "-c", "user.name=T", "commit", "-q", "-m", "theirs")
    integration.merge(gated["integration"], "cortex/other", message="theirs")
    head_before = gitutil.head(gated["integration"].path)

    (gated["tree"].path / "shared.py").write_text("ours = 2\n", encoding="utf-8")
    report = verification.verify(
        conn, gated["task"], project=gated["project"], workspace=gated["tree"].path,
        before=gated["before"], producer="claude", review_fn=_approve, test_fn=_tests(True),
    )

    assert report.status == "needs_owner"
    merge = next(check for check in report.checks if check.name == "merge")
    assert merge.status == verification.OWNER
    assert "shared.py" in merge.evidence["conflicts"]
    assert gitutil.head(gated["integration"].path) == head_before


def test_a_secret_in_the_diff_is_rejected(gated, conn):
    (gated["tree"].path / "settings.py").write_text(
        'AWS_SECRET_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        'token = "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB"\n',
        encoding="utf-8",
    )

    report = verification.verify(
        conn, gated["task"], project=gated["project"], workspace=gated["tree"].path,
        before=gated["before"], producer="claude", review_fn=_approve, test_fn=_tests(True),
    )

    assert report.status == "rejected"
    assert next(c for c in report.checks if c.name == "secrets").status == verification.FAIL


def test_missing_test_command_is_an_absence_not_a_pass(conn, git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_ROOT", str(tmp_path / "work"))
    project_id = store.create_project(
        conn, name="Untested", repo_path=str(git_repo), test_command=None,
    )
    project = store.get_project(conn, project_id)
    task_id = store.create_task(conn, project_id=project_id, title="Work", type="code")
    integration.ensure(git_repo, project_id)
    tree = worktrees.ensure(git_repo, project_id, task_id, base=integration.BRANCH)
    before = gitutil.head(tree.path)
    (tree.path / "feature.py").write_text("value = 1\n", encoding="utf-8")

    report = verification.verify(
        conn, store.get_task(conn, task_id), project=project, workspace=tree.path,
        before=before, producer="claude", review_fn=_approve,
    )

    tests = [check for check in report.checks if check.name.endswith("tests")]
    assert [check.status for check in tests] == [verification.SKIPPED, verification.SKIPPED]
    # Skipped is not blocking: an untested project can still merge, and the
    # owner can see from the record that nothing was proven by tests.
    assert report.status == "merged"


def test_path_and_protected_matching_handles_globs():
    assert verification.path_violations(["src/a.py"], '["src/**"]') == []
    assert verification.path_violations(["src/deep/a.py"], '["src/**/*.py"]') == []
    assert verification.path_violations(["other.py"], '["src/**"]') == ["other.py"]
    assert verification.path_violations(["anything"], None) == []
    protected = verification.protected_changes(
        ["package-lock.json", ".claude/settings.json", "src/app.py"]
    )
    assert [item["path"] for item in protected] == ["package-lock.json", ".claude/settings.json"]


def test_a_reviewer_is_found_in_a_fresh_process(conn, project, monkeypatch):
    """Found by a real run: the gate had no reviewer on a ready machine.

    The gate runs once, in its own process, so the non-blocking probe cache is
    always cold there and reports every worker as "checking". Choosing a
    reviewer has to ask for a real answer.
    """
    from cortex import workers

    monkeypatch.setattr(workers, "_PROBE_CACHE", {})
    monkeypatch.setattr(
        workers, "probe",
        lambda name, **kwargs: {"availability": "ready" if name == "codex" else "missing"},
    )

    assert review.choose_reviewer(
        conn, project, exclude="opencode", unattended=False
    ) == ("codex", "default")
    # The producing worker is never its own reviewer, even when it is the only
    # one ready.
    monkeypatch.setattr(
        workers, "probe",
        lambda name, **kwargs: {"availability": "ready" if name == "opencode" else "missing"},
    )
    assert review.choose_reviewer(conn, project, exclude="opencode", unattended=False) is None


def test_the_review_prompt_is_built_from_a_real_task_row(conn, project):
    """Found by a real run: the prompt read a column tasks do not have.

    Every gate test stubs the reviewer, so nothing exercised build_prompt
    against an actual row until a live run crashed on it after eight checks
    had already passed.
    """
    task_id = store.create_task(
        conn, project_id=project["id"], title="Add a sentence_count helper",
        type="code", brief="Add sentence_count(text) to src/textstats.py.",
        acceptance="sentence_count counts . ! and ? endings.",
    )
    prompt = review.build_prompt(
        store.get_task(conn, task_id), diff="--- a\n+++ b\n", 
        changed_files=["src/textstats.py"], producer="opencode",
    )

    assert "Add a sentence_count helper" in prompt
    assert "Add sentence_count(text) to src/textstats.py." in prompt
    assert "sentence_count counts . ! and ? endings." in prompt
    assert "Produced by: opencode" in prompt
    assert "VERDICT: pass" in prompt


def test_a_retried_task_is_judged_on_its_whole_branch(gated, conn):
    """Found by a real run: a reused worktree made finished work invisible.

    A task worktree survives a rejected attempt. On the retry the agent sees
    its own earlier commit, correctly reports there is nothing left to do, and
    the gate must still judge the branch against the integration branch rather
    than against the revision this particular run started from.
    """
    tree = gated["tree"].path
    (tree / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "-c", "user.email=a@b.c", "-c", "user.name=Agent", "commit", "-q", "-m", "earlier attempt")
    resumed_head = gitutil.head(tree)

    # The second run starts from the earlier attempt's commit and adds nothing.
    report = verification.verify(
        conn, gated["task"], project=gated["project"], workspace=tree,
        before=resumed_head, producer="claude", review_fn=_approve, test_fn=_tests(True),
    )

    assert report.status == "merged"
    changes = next(check for check in report.checks if check.name == "changes")
    assert changes.evidence["files"] == ["feature.py"]
    assert (gated["integration"].path / "feature.py").exists()


def test_a_crash_after_the_merge_rewinds_the_branch(gated, conn):
    """Found by a real run: a gate bug left an unjudged merge on the branch.

    The reviewer step raised before any verdict existed. The merge stayed, no
    verification was recorded, and the next run saw the work as already
    integrated. Whatever fails after the merge, the branch goes back.
    """
    (gated["tree"].path / "feature.py").write_text("value = 1\n", encoding="utf-8")
    head_before = gitutil.head(gated["integration"].path)

    def explode(*args, **kwargs):
        raise IndexError("No item with that key")

    report = verification.verify(
        conn, gated["task"], project=gated["project"], workspace=gated["tree"].path,
        before=gated["before"], producer="claude", review_fn=explode, test_fn=_tests(True),
    )

    assert report.status == "error"
    assert report.merge_commit is None
    assert gitutil.head(gated["integration"].path) == head_before
    assert not (gated["integration"].path / "feature.py").exists()
    # The failure is recorded and surfaced, not swallowed.
    assert "IndexError" in report.summary()
    assert verification.get(conn, report.verification_id)["status"] == "error"
    assert any(item["kind"] == "verification_rejected" for item in inbox.items(conn))


def test_a_small_change_is_reviewed_by_a_cheap_model_first(conn, project, monkeypatch):
    """Owner decision 2026-09-18: premium review only where it earns its cost."""
    from cortex import workers

    monkeypatch.setattr(workers, "_PROBE_CACHE", {})
    monkeypatch.setattr(workers, "probe", lambda name, **kwargs: {"availability": "ready"})

    small = "+one line\n"
    large = "".join(f"+line {n}\n" for n in range(review.SMALL_DIFF_LINES + 1))

    assert review.tier_for(small, premium_required=False)[0] in review.CHEAP
    assert review.tier_for(large, premium_required=False)[0] in review.PREMIUM
    # High risk overrides size: a one-line change to something dangerous still
    # gets the better judge.
    assert review.tier_for(small, premium_required=True)[0] in review.PREMIUM

    assert review.choose_reviewer(
        conn, project, exclude="codex", unattended=False,
        order=review.tier_for(small, premium_required=False),
    ) == ("ollama", "phi4:14b")


def test_a_high_risk_task_is_never_cheaply_reviewed(gated, conn):
    store.update_task(conn, gated["task"]["id"], risk="high")
    (gated["tree"].path / "feature.py").write_text("value = 1\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def record(*args, **kwargs):
        seen.update(kwargs)
        return review.ReviewOutcome("pass", "codex", "default", ("fine",))

    verification.verify(
        conn, store.get_task(conn, gated["task"]["id"]), project=gated["project"],
        workspace=gated["tree"].path, before=gated["before"], producer="claude",
        review_fn=record, test_fn=_tests(True),
    )

    assert seen["premium_required"] is True
