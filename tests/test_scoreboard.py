"""Phase 4 measurement: cost per accepted task, told honestly."""

from __future__ import annotations

import json

import pytest

from cortex import ids, scoreboard, store, verification


def _run(
    conn, project, task_id, *, worker="opencode", model="deepseek", exit_code=0,
    outcome=None, tokens=(1000, 200), started="2026-09-17T10:00:00Z",
    ended="2026-09-17T10:02:00Z",
):
    run_id = store.create_run(
        conn, task_id=task_id, project_id=project["id"], model=f"{worker}:{model}",
        execution_mode="headless_cli",
    )
    conn.execute(
        "UPDATE runs SET started_at = ?, ended_at = ?, exit_code = ?, outcome = ?, usage_json = ? "
        "WHERE id = ?",
        (
            started, ended, exit_code, outcome,
            json.dumps({"input_tokens": tokens[0], "output_tokens": tokens[1]}),
            run_id,
        ),
    )
    conn.commit()
    return run_id


def _task(conn, project, title="Work", task_type="code"):
    return store.create_task(conn, project_id=project["id"], title=title, type=task_type)


def test_a_cell_reports_cost_per_accepted_task(conn, project):
    task_id = _task(conn, project)
    for index in range(4):
        _run(conn, project, task_id, outcome="accepted" if index < 2 else None,
             tokens=(1000, 0))

    cell = scoreboard.cells(conn)[0]

    assert (cell.worker, cell.model, cell.task_type) == ("opencode", "deepseek", "code")
    assert cell.attempts == 4 and cell.completed == 4 and cell.accepted == 2
    assert cell.total_tokens == 4000
    # Two accepted tasks out of 4000 tokens spent: 2000 per accepted task.
    assert cell.tokens_per_accepted == 2000
    assert cell.acceptance_rate == 50.0
    assert cell.median_seconds == 120


def test_spending_with_nothing_accepted_is_unproven_not_infinite(conn, project):
    task_id = _task(conn, project)
    _run(conn, project, task_id, tokens=(40_000, 500))

    cell = scoreboard.cells(conn)[0]

    assert cell.total_tokens == 40_500
    assert cell.tokens_per_accepted is None
    assert cell.acceptance_rate == 0.0


def test_a_rate_carries_its_sample_size(conn, project):
    task_id = _task(conn, project)
    _run(conn, project, task_id, outcome="accepted")

    cell = scoreboard.cells(conn)[0]
    assert cell.acceptance_rate == 100.0
    assert cell.proven is False

    for _ in range(scoreboard.PROVEN_AFTER - 1):
        _run(conn, project, task_id, outcome="accepted")
    assert scoreboard.cells(conn)[0].proven is True


def test_gate_and_review_results_are_counted_separately_from_acceptance(conn, project):
    task_id = _task(conn, project)
    merged_run = _run(conn, project, task_id)
    rejected_run = _run(conn, project, task_id)
    for run_id, status, review_status in (
        (merged_run, "merged", "pass"),
        (rejected_run, "rejected", "fail"),
    ):
        report = verification.GateReport(
            task_id=task_id, project_id=project["id"], run_id=run_id, status=status,
        )
        report.checks = [
            verification.Check("merge", "pass", "merged"),
            verification.Check(
                "review", review_status, "codex",
                {"reviewer": "codex", "usage": {"input_tokens": 5000, "output_tokens": 100}},
            ),
        ]
        verification.record(conn, report)

    cell = scoreboard.cells(conn)[0]

    assert cell.gate_judged == 2 and cell.gate_passed == 1
    assert cell.review_judged == 2 and cell.review_passed == 1
    assert cell.review_pass_rate == 50.0
    # The gate merged one change, but the owner has accepted nothing: these are
    # different questions and the scoreboard keeps them apart.
    assert cell.accepted == 0 and cell.acceptance_rate == 0.0


def test_review_tokens_are_charged_to_the_reviewer_not_the_producer(conn, project):
    task_id = _task(conn, project)
    run_id = _run(conn, project, task_id, worker="opencode", tokens=(1000, 100))
    report = verification.GateReport(
        task_id=task_id, project_id=project["id"], run_id=run_id, status="merged",
    )
    report.checks = [verification.Check(
        "review", "pass", "codex approved",
        {"reviewer": "codex", "usage": {"input_tokens": 80_000, "output_tokens": 200}},
    )]
    verification.record(conn, report)

    cell = scoreboard.cells(conn)[0]
    overhead = scoreboard.review_overhead(conn)

    # The cheap implementer is not made to look expensive by its reviewer.
    assert cell.total_tokens == 1100
    assert overhead["reviewers"] == [
        {"reviewer": "codex", "reviews": 1, "input_tokens": 80_000,
         "output_tokens": 200, "cached_tokens": 0}
    ]
    assert overhead["total_tokens"] == 80_200


def test_best_for_only_considers_proven_rows(conn, project):
    task_id = _task(conn, project)
    # A single lucky, very cheap run must not win the table.
    _run(conn, project, task_id, worker="ollama", model="phi4", outcome="accepted",
         tokens=(10, 1))
    for _ in range(scoreboard.PROVEN_AFTER):
        _run(conn, project, task_id, worker="opencode", model="deepseek",
             outcome="accepted", tokens=(1000, 100))

    best = scoreboard.best_for(conn, "code")

    assert best is not None and best.worker == "opencode"
    assert scoreboard.best_for(conn, "research") is None


def test_payload_states_its_own_caveats(conn, project):
    task_id = _task(conn, project)
    _run(conn, project, task_id, outcome="accepted")

    payload = scoreboard.payload(conn)

    assert payload["totals"] == {
        "attempts": 1, "accepted": 1, "tokens": 1200, "proven_cells": 0
    }
    assert "anecdotes" in payload["note"]
    assert payload["proven_after"] == scoreboard.PROVEN_AFTER


def test_a_window_excludes_older_runs(conn, project):
    task_id = _task(conn, project)
    _run(conn, project, task_id, started="2026-01-01T10:00:00Z", ended="2026-01-01T10:01:00Z")
    _run(conn, project, task_id, started=ids.now(), ended=ids.now())

    assert scoreboard.cells(conn)[0].attempts == 2
    assert sum(cell.attempts for cell in scoreboard.cells(conn, since_days=7)) == 1


# -- the owner's ranking rule --------------------------------------------------

def _proven(conn, project, *, worker, model, tokens, runs=scoreboard.PROVEN_AFTER,
            task_type="code"):
    task_id = _task(conn, project, task_type=task_type)
    for _ in range(runs):
        _run(conn, project, task_id, worker=worker, model=model, outcome="accepted",
             tokens=tokens)
    return task_id


def test_the_cheapest_proven_worker_takes_the_work(conn, project):
    _proven(conn, project, worker="codex", model="default", tokens=(20_000, 500))
    _proven(conn, project, worker="opencode", model="deepseek", tokens=(2_000, 100))

    cell = scoreboard.cheapest_proven(conn, project, "code", action="implement")

    assert cell is not None and cell.worker == "opencode"


def test_an_unproven_bargain_does_not_win(conn, project):
    _proven(conn, project, worker="codex", model="default", tokens=(20_000, 500))
    _proven(conn, project, worker="ollama", model="phi4", tokens=(10, 1), runs=1)

    cell = scoreboard.cheapest_proven(conn, project, "code", action="implement")

    assert cell is not None and cell.worker == "codex"


def test_a_tie_is_left_to_the_existing_rules(conn, project):
    _proven(conn, project, worker="codex", model="default", tokens=(1_000, 100))
    _proven(conn, project, worker="claude", model="sonnet", tokens=(1_000, 100))

    assert scoreboard.cheapest_proven(conn, project, "code", action="implement") is None


def test_a_worker_the_project_forbids_is_never_proposed(conn, project):
    _proven(conn, project, worker="codex", model="default", tokens=(20_000, 500))
    _proven(conn, project, worker="opencode", model="deepseek", tokens=(2_000, 100))
    store.update_project(conn, project["id"], allowed_workers='["codex"]')
    allowed = store.get_project(conn, project["id"])

    cell = scoreboard.cheapest_proven(conn, allowed, "code", action="implement")

    assert cell is not None and cell.worker == "codex"


def test_sourced_research_is_left_alone(conn, project):
    _proven(conn, project, worker="opencode", model="deepseek", tokens=(2_000, 100),
            task_type="research")

    assert scoreboard.cheapest_proven(conn, project, "research", action="research") is None


def test_an_explicit_assignee_outranks_the_measurement(conn, project):
    from cortex import dispatcher, routing

    _proven(conn, project, worker="opencode", model="deepseek", tokens=(2_000, 100))
    task_id = store.create_task(
        conn, project_id=project["id"], title="Write the adapter", type="code",
        complexity=8, risk="high",
    )
    task = store.get_task(conn, task_id)
    route = routing.effective_route(project, task)

    # With no assignee, the measured cheaper worker takes it and says why.
    swapped = dispatcher.with_scoreboard(conn, project, task, route)
    assert swapped.worker == "opencode"
    assert any("scoreboard" in reason for reason in swapped.reasons)

    store.update_task(conn, task_id, assignee="codex")
    instructed = store.get_task(conn, task_id)
    kept = dispatcher.with_scoreboard(
        conn, project, instructed, routing.effective_route(project, instructed)
    )
    assert kept.worker == "codex"
