"""Worker allowlist: the single place privacy is enforced."""

from __future__ import annotations

import pytest

from cortex import dispatcher, policy, routing, store, team


def _task(conn, project, **kwargs):
    defaults = dict(title="Review the launch plan", type="review", complexity=5)
    defaults.update(kwargs)
    tid = store.create_task(conn, project_id=project["id"], **defaults)
    return store.get_task(conn, tid)


def test_unconfigured_restricted_project_stays_local(conn, project):
    store.update_project(conn, project["id"], privacy="restricted")
    restricted = store.get_project(conn, project["id"])
    assert policy.allowed_workers(restricted) == ("ollama",)
    assert not policy.is_configured(restricted)


def test_unconfigured_internal_project_allows_everything(conn, project):
    assert set(policy.allowed_workers(project)) == set(policy.ALL_WORKERS)


def test_explicit_allowlist_overrides_the_privacy_default(conn, project):
    store.update_project(conn, project["id"], privacy="restricted")
    store.update_project(
        conn, project["id"], allowed_workers=policy.encode(["claude", "ollama"])
    )
    updated = store.get_project(conn, project["id"])
    assert policy.allowed_workers(updated) == ("claude", "ollama")
    assert policy.is_configured(updated)
    assert policy.is_allowed(updated, "claude")
    assert not policy.is_allowed(updated, "codex")


def test_encode_rejects_unknown_and_empty_worker_lists():
    with pytest.raises(ValueError):
        policy.encode(["not-a-worker"])
    with pytest.raises(ValueError):
        policy.encode([])


def test_routing_substitutes_a_permitted_worker(conn, project):
    """A review that would go to Claude runs on Ollama when that is all we allow."""
    store.update_project(
        conn, project["id"], privacy="restricted",
        allowed_workers=policy.encode(["ollama"]),
    )
    restricted = store.get_project(conn, project["id"])
    route = routing.route_task(restricted, _task(conn, restricted))
    assert route.worker == "ollama"
    # Restricted privacy raises the task to high risk, which routes to Codex.
    assert route.preferred_worker == "codex"
    assert "allowlist" in (route.policy_note or "")
    # The substituted worker must not inherit the other worker's model name.
    assert route.model == "phi4:14b"


def test_substitution_keeps_an_explicitly_requested_model(conn, project):
    store.update_project(
        conn, project["id"], allowed_workers=policy.encode(["claude", "ollama"])
    )
    allowed = store.get_project(conn, project["id"])
    task = _task(conn, allowed, requested_model="opus")
    assert routing.route_task(allowed, task).model == "opus"


def test_effective_route_prefers_the_stored_assignee(conn, project):
    task = _task(conn, project, assignee="grok")
    assert routing.effective_route(project, task).worker == "grok"
    assert routing.effective_route(project, task).model == "default"


def test_effective_route_substitutes_a_disallowed_assignee(conn, project):
    """A stale assignment is a preference, not a dead end."""
    store.update_project(
        conn, project["id"], privacy="restricted",
        allowed_workers=policy.encode(["claude", "ollama"]),
    )
    restricted = store.get_project(conn, project["id"])
    task = _task(conn, restricted, assignee="codex")
    route = routing.effective_route(restricted, task)
    assert route.worker == "claude"
    assert route.preferred_worker == "codex"


def test_dispatch_refuses_a_caller_override_that_is_not_permitted(conn, project):
    """An explicit instruction errors rather than silently running elsewhere."""
    store.update_project(
        conn, project["id"], allowed_workers=policy.encode(["ollama"])
    )
    task = _task(conn, store.get_project(conn, project["id"]))
    with pytest.raises(dispatcher.DispatchError, match="not on .* allowlist"):
        dispatcher.dispatch(conn, task, worker_override="codex", timeout=5)


def test_high_risk_read_only_work_no_longer_needs_extra_approval(conn, project):
    """Risk gates blast radius via allow_write, not visibility."""
    store.update_project(conn, project["id"], privacy="restricted")
    restricted = store.get_project(conn, project["id"])
    task = _task(conn, restricted, risk="high", type="review")
    route = routing.route_task(restricted, task)
    assert route.worker == "ollama"
    assert route.blocked_reason is None


def test_keep_working_sees_tasks_assigned_to_a_disallowed_worker(
    conn, project, monkeypatch
):
    """The regression that left six tasks unstartable."""
    monkeypatch.setattr(
        team.workers, "probe", lambda name, **kw: {"availability": "ready", "note": None}
    )
    store.update_project(
        conn, project["id"], privacy="restricted",
        allowed_workers=policy.encode(["claude", "ollama"]),
    )
    restricted = store.get_project(conn, project["id"])
    task = _task(conn, restricted, assignee="codex")
    store.update_task(conn, task["id"], status="assigned")
    assert team.safe_start_candidates(conn, limit=3) == [task["id"]]
