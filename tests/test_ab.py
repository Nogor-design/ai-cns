"""Phase 4's exit criterion, as a measurement that can come out either way."""

from __future__ import annotations

import json

import pytest

from cortex import ab, ids, store, verification

SPECS = [
    ab.TaskSpec("Add a parser", "Parse the sample", "parser.py parses the sample"),
    ab.TaskSpec("Add a formatter", "Format the output", "formatter.py formats"),
]


def _runner(*, tokens, accept=True, review_tokens=0):
    """A fake arm: each task gets one run, optionally merged by the gate."""
    def run(conn, task_id):
        task = store.get_task(conn, task_id)
        run_id = store.create_run(
            conn, task_id=task_id, project_id=task["project_id"],
            model="opencode:deepseek", execution_mode="headless_cli",
        )
        conn.execute(
            "UPDATE runs SET started_at=?, ended_at=?, exit_code=0, usage_json=? WHERE id=?",
            (
                "2026-09-18T10:00:00Z", "2026-09-18T10:01:00Z",
                json.dumps({"input_tokens": tokens, "output_tokens": 0}), run_id,
            ),
        )
        conn.commit()
        report = verification.GateReport(
            task_id=task_id, project_id=task["project_id"], run_id=run_id,
            status="merged" if accept else "rejected",
        )
        report.checks = [verification.Check(
            "review", "pass" if accept else "fail", "codex",
            {"reviewer": "codex", "usage": {"input_tokens": review_tokens, "output_tokens": 0}},
        )]
        verification.record(conn, report)
    return run


def _arm(conn, project, name, **kwargs):
    return ab.run_arm(
        conn, name=name, project_id=project["id"], specs=SPECS,
        runner=_runner(**kwargs), settings={"arm": name},
    )


def test_each_arm_gets_its_own_copy_of_the_same_tasks(conn, project):
    baseline = _arm(conn, project, "baseline", tokens=1000)
    candidate = _arm(conn, project, "candidate", tokens=500)

    assert baseline.tasks == candidate.tasks == len(SPECS)
    assert set(baseline.task_ids).isdisjoint(candidate.task_ids)
    titles = [store.get_task(conn, task_id)["title"] for task_id in candidate.task_ids]
    assert titles == [f"[candidate] {spec.title}" for spec in SPECS]


def test_a_cheaper_arm_that_is_still_accepted_wins(conn, project):
    baseline = _arm(conn, project, "baseline", tokens=10_000)
    candidate = _arm(conn, project, "candidate", tokens=2_000)

    comparison = ab.compare(baseline, candidate)

    assert comparison.verdict == "better"
    assert comparison.token_change == -80.0
    assert "80.0% fewer tokens per accepted task" in comparison.summary()


def test_cheaper_but_less_accepted_is_not_a_win(conn, project):
    baseline = _arm(conn, project, "baseline", tokens=10_000)
    candidate = _arm(conn, project, "candidate", tokens=100, accept=False)

    comparison = ab.compare(baseline, candidate)

    # Nothing was accepted, so there is no cost per accepted task to compare.
    assert comparison.verdict == "not_proven"
    assert "accepted nothing" in comparison.summary()


def test_a_partial_drop_in_acceptance_loses_despite_cheaper_tokens(conn, project):
    baseline = _arm(conn, project, "baseline", tokens=10_000)

    # The candidate is five times cheaper but only half its work is accepted.
    accepted_once = {"done": False}

    def flaky(conn, task_id):
        first = not accepted_once["done"]
        accepted_once["done"] = True
        _runner(tokens=1_000, accept=first)(conn, task_id)

    candidate = ab.run_arm(
        conn, name="candidate", project_id=project["id"], specs=SPECS, runner=flaky,
    )

    comparison = ab.compare(baseline, candidate)

    assert candidate.accepted == 1 and baseline.accepted == 2
    assert candidate.tokens_per_accepted == 2_000     # cheaper per accepted task
    assert candidate.acceptance_rate == 50.0          # but accepted half as often
    assert comparison.verdict == "worse"
    assert "acceptance fell" in comparison.summary()


def test_a_small_difference_is_called_even(conn, project):
    baseline = _arm(conn, project, "baseline", tokens=10_000)
    candidate = _arm(conn, project, "candidate", tokens=9_500)

    comparison = ab.compare(baseline, candidate)

    assert comparison.verdict == "even"
    assert "noise floor" in comparison.summary()


def test_review_tokens_count_against_the_arm_that_spent_them(conn, project):
    baseline = _arm(conn, project, "baseline", tokens=5_000, review_tokens=0)
    candidate = _arm(conn, project, "candidate", tokens=1_000, review_tokens=80_000)

    # A cheap implementer reviewed expensively is not a saving, and the
    # comparison says so because the review is part of what the change cost.
    assert baseline.tokens_per_accepted == 5_000
    assert candidate.tokens_per_accepted == 81_000
    assert ab.compare(baseline, candidate).verdict == "worse"


def test_the_comparison_is_kept_for_the_owner(conn, project):
    baseline = _arm(conn, project, "baseline", tokens=10_000)
    candidate = _arm(conn, project, "candidate", tokens=2_000)

    ab.record(conn, project["id"], ab.compare(baseline, candidate), note="skill cards on")
    stored = ab.latest(conn)

    assert stored and stored[0]["verdict"] == "better"
    assert stored[0]["note"] == "skill cards on"
    assert stored[0]["candidate"]["tokens_per_accepted"] == 2_000
