"""Phase 1 guardrails: protected projects, quota admission, local lanes, adapters."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex import (
    autonomy, capacity, dispatcher, lanes, policy, store, team, workers,
)


@pytest.fixture(autouse=True)
def empty_hub_registry(tmp_path, monkeypatch):
    registry = tmp_path / "capabilities.json"
    registry.write_text(json.dumps({"capabilities": []}), encoding="utf-8")
    monkeypatch.setenv(autonomy.HUB_REGISTRY_ENV, str(registry))
    monkeypatch.setenv(capacity.CODEX_SESSIONS_ENV, str(tmp_path / "no-sessions"))
    return registry


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project(conn, name, repo, **fields):
    pid = store.create_project(conn, name=name, repo_path=repo, **fields)
    return store.get_project(conn, pid)


# ------------------------------------------------------------- autonomy ---
def test_trading_and_apollo_projects_are_protected(conn, tmp_path, empty_hub_registry):
    empty_hub_registry.write_text(json.dumps({"capabilities": [
        {"id": "x", "owner_path": r"D:\some-quant-lab"},
    ]}), encoding="utf-8")
    cases = {
        "hub": _project(conn, "Research Hub", r"D:\trading-capability-hub"),
        "program": _project(conn, "Plain", r"D:\plain", program="trading-systems"),
        "registry": _project(conn, "Quant Lab", r"D:\some-quant-lab\sub"),
        "apollo": _project(conn, "apollo-ats", r"D:\apollo\apollo-ats"),
        "named": _project(conn, "trader-dan-landing", r"D:\trader-dan-landing"),
    }
    assert autonomy.protection(cases["hub"]).reason == "trading"
    assert autonomy.protection(cases["program"]).reason == "trading"
    assert autonomy.protection(cases["registry"]).reason == "trading"
    assert autonomy.protection(cases["apollo"]).reason == "apollo"
    assert autonomy.protection(cases["named"]).reason == "trading"
    for project in cases.values():
        assert autonomy.mode(project) == "off"
        with pytest.raises(ValueError, match="protected"):
            autonomy.validate_mode_change(project, "read_only")
        assert autonomy.validate_mode_change(project, "off") == "off"


def test_ordinary_project_defaults_to_read_only(conn, project):
    assert autonomy.protection(project) is None
    assert autonomy.mode(project) == "read_only"
    assert autonomy.unattended_refusal(conn, project, write=False) is None
    assert "read-only" in autonomy.unattended_refusal(conn, project, write=True)
    store.update_project(conn, project["id"], autonomy_mode="integration")
    updated = store.get_project(conn, project["id"])
    assert autonomy.unattended_refusal(conn, updated, write=True) is None


def test_pause_and_off_block_unattended_work(conn, project):
    store.update_project(conn, project["id"], autonomy_mode="off")
    off = store.get_project(conn, project["id"])
    assert "turned off" in autonomy.unattended_refusal(conn, off, write=False)
    autonomy.set_paused(conn, True)
    assert "paused" in autonomy.unattended_refusal(conn, project, write=False)
    with pytest.raises(ValueError):
        autonomy.validate_mode_change(project, "everything")


# ------------------------------------------------------------- capacity ---
CODEX_LIMITS = {
    "limit_id": "codex",
    "primary": {"used_percent": 78.0, "window_minutes": 10080, "resets_at": 1789833950},
    "secondary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1789671000},
    "rate_limit_reached_type": None,
}
CLAUDE_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.29, "resetsAt": 1789671000},
            "seven_day": {"utilization": 0.14, "resetsAt": 1789873200},
        },
    },
}


def test_parses_codex_and_claude_signals():
    codex = capacity.parse_codex_rate_limits(CODEX_LIMITS, source="test")
    assert {(r.window, r.used_percent) for r in codex} == {
        ("seven_day", 78.0), ("five_hour", 12.0)
    }
    assert codex[0].resets_at == "2026-09-19T16:05:50Z"
    output = "\n".join([
        json.dumps({"type": "system"}),
        json.dumps(CLAUDE_EVENT),
        json.dumps({"type": "result", "is_error": False, "result": "ok"}),
    ])
    claude = capacity.readings_from_output("claude", output, exit_code=0)
    assert {(r.window, r.used_percent) for r in claude} == {
        ("five_hour", 29.0), ("seven_day", 14.0)
    }
    assert not any(r.limited for r in claude)


def test_refusal_starts_a_cooldown(conn):
    output = json.dumps({
        "type": "result", "is_error": True, "api_error_status": 429,
        "result": "Claude usage limit reached",
    })
    readings = capacity.readings_from_output("claude", output, exit_code=1)
    assert readings and readings[0].limited
    capacity.record(conn, readings)
    admission = capacity.admit(conn, "claude")
    assert not admission.allowed
    assert "cooling down" in admission.reason
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert capacity.admit(conn, "claude", now=later).allowed


def test_reserve_ceiling_controls_metered_admission(conn):
    capacity.record(conn, [capacity.Reading(
        provider="codex", window="seven_day", used_percent=60.0,
        resets_at=_iso(timedelta(days=2)), window_minutes=10080,
    )])
    assert capacity.admit(conn, "codex").allowed          # 60 + 2 <= 70
    capacity.set_reserve_pct(conn, 40)
    blocked = capacity.admit(conn, "codex")               # 60 + 2 > 60
    assert not blocked.allowed
    assert "keep 40% for you" in blocked.reason
    with pytest.raises(ValueError):
        capacity.set_reserve_pct(conn, 99)


def test_expired_window_counts_as_reset(conn):
    capacity.record(conn, [capacity.Reading(
        provider="codex", window="five_hour", used_percent=95.0,
        resets_at=_iso(timedelta(minutes=-5)),
    )])
    windows = capacity.current_windows(conn, "codex")
    assert windows[0]["used_percent"] == 0.0 and windows[0]["estimated"]
    assert capacity.admit(conn, "codex").allowed


def test_record_skips_unchanged_and_older_readings(conn):
    reading = capacity.Reading(
        provider="codex", window="seven_day", used_percent=50.0,
        resets_at=_iso(timedelta(days=1)), observed_at=_iso(timedelta(0)),
    )
    assert capacity.record(conn, [reading]) == 1
    assert capacity.record(conn, [reading]) == 0
    older = capacity.Reading(**{
        **reading.__dict__, "used_percent": 40.0,
        "observed_at": _iso(timedelta(hours=-1)),
    })
    assert capacity.record(conn, [older]) == 0


def test_unknown_quota_allows_one_unattended_run_at_a_time(conn, project):
    assert capacity.admit(conn, "claude").allowed
    task_id = store.create_task(conn, project_id=project["id"], title="Review")
    run_id = store.create_run(
        conn, task_id=task_id, project_id=project["id"],
        model="claude:sonnet", execution_mode="headless_cli",
    )
    store.update_run(conn, run_id, started_by="scheduler")
    admission = capacity.admit(conn, "claude")
    assert not admission.allowed and "one unattended run at a time" in admission.reason


def test_counted_providers_have_a_rolling_cap(conn, project):
    conn.execute("INSERT INTO settings VALUES (?, ?, ?)", (capacity.CALL_CAP_KEY, "2", "now"))
    task_id = store.create_task(conn, project_id=project["id"], title="Review")
    for _ in range(2):
        run_id = store.create_run(
            conn, task_id=task_id, project_id=project["id"],
            model="grok:default", execution_mode="headless_cli",
        )
        store.update_run(conn, run_id, started_by="scheduler", ended_at="done")
    admission = capacity.admit(conn, "grok")
    assert not admission.allowed and "2 of 2" in admission.reason
    assert capacity.admit(conn, "gemini").allowed


def test_scan_codex_sessions_reads_latest_tail(tmp_path):
    day = tmp_path / "2026" / "09" / "17"
    day.mkdir(parents=True)
    old = day / "rollout-old.jsonl"
    new = day / "rollout-new.jsonl"
    event = lambda pct, ts: json.dumps({  # noqa: E731
        "timestamp": ts, "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": pct, "window_minutes": 10080, "resets_at": 1789833950},
        }},
    })
    old.write_text(event(10.0, "2026-09-17T08:00:00.000Z") + "\n", encoding="utf-8")
    new.write_text(
        event(70.0, "2026-09-17T09:00:00.000Z") + "\n"
        + json.dumps({"type": "other"}) + "\n"
        + event(79.0, "2026-09-17T09:30:00.000Z") + "\n",
        encoding="utf-8",
    )
    readings = capacity.scan_codex_sessions(tmp_path)
    assert len(readings) == 1
    assert readings[0].used_percent == 79.0
    assert readings[0].observed_at == "2026-09-17T09:30:00Z"


# ---------------------------------------------------------------- lanes ---
def test_model_classification():
    gb = 1024 ** 3
    common = dict(vram_mb=12288, ram_mb=131072)
    assert lanes.classify(size_bytes=9 * gb, family="phi3", is_moe=False, **common) == "gpu"
    assert lanes.classify(size_bytes=18 * gb, family="qwen3moe", is_moe=True, **common) == "hybrid_moe"
    assert lanes.classify(size_bytes=14 * gb, family="llama", is_moe=False, **common) == "hybrid_dense"
    assert lanes.classify(size_bytes=19 * gb, family="qwen2", is_moe=False, **common) == "avoid"
    assert lanes.classify(size_bytes=65 * gb, family="gptoss", is_moe=True, **common) == "cpu_batch"
    assert lanes.classify(size_bytes=90 * gb, family="mixtral", is_moe=True, **common) == "avoid"
    assert lanes.classify(size_bytes=gb // 3, family="nomic-bert", is_moe=False, **common) == "embedding"


def test_local_admission_respects_slot_and_ninjatrader(conn, project, monkeypatch):
    lanes_by_model = {"small": "gpu", "coder": "hybrid_moe", "huge": "cpu_batch"}
    monkeypatch.setattr(lanes, "lane_for", lambda model: lanes_by_model.get(model))
    monkeypatch.setattr(lanes, "ninjatrader_running", lambda max_age=15: True)
    assert lanes.admit(conn, "small")[0]
    allowed, why = lanes.admit(conn, "coder")
    assert not allowed and "NinjaTrader" in why
    assert not lanes.admit(conn, "huge")[0]
    assert not lanes.admit(conn, "missing")[0]
    monkeypatch.setattr(lanes, "ninjatrader_running", lambda max_age=15: False)
    assert lanes.admit(conn, "coder")[0]

    task_id = store.create_task(conn, project_id=project["id"], title="Local")
    store.create_run(
        conn, task_id=task_id, project_id=project["id"],
        model="ollama:small", execution_mode="headless_cli",
    )
    allowed, why = lanes.admit(conn, "small")
    assert not allowed and "slot is busy" in why


# ------------------------------------------------------------- adapters ---
def test_agy_command_points_at_brief_and_stays_read_only(tmp_path):
    brief = tmp_path / "run" / "brief.md"
    spec = workers.build_command(
        worker="agy", model="default", action="review", workspace=tmp_path,
        brief_path=brief, budget="small", effort="xhigh",
    )
    argv = list(spec.argv)
    assert "--sandbox" in argv and "--mode" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--add-dir") + 1] == str(brief.parent)
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[-1].startswith("--print=") and str(brief) in argv[-1]
    write = workers.build_command(
        worker="agy", model="default", action="implement", workspace=tmp_path,
        brief_path=brief, budget="small",
    )
    assert write.argv[write.argv.index("--mode") + 1] == "accept-edits"


def test_opencode_is_local_only(tmp_path):
    spec = workers.build_command(
        worker="opencode", model="default", action="review", workspace=tmp_path,
        brief_path=tmp_path / "brief.md", budget="small",
    )
    argv = list(spec.argv)
    assert argv[argv.index("--model") + 1] == "ollama/qwen3-coder:30b"
    assert argv[argv.index("--agent") + 1] == "plan"
    # --file takes a list, so the message must come before it.
    assert argv[2].startswith("Follow the complete task brief")
    assert argv.index("--file") == len(argv) - 2
    config_path = Path(dict(spec.env)["OPENCODE_CONFIG"])
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert list(document["provider"]) == ["ollama"]
    assert document["provider"]["ollama"]["options"]["baseURL"].endswith("/v1")
    assert workers.opencode_model("ollama/gpt-oss:20b") == "gpt-oss:20b"
    assert workers.opencode_model("trading-hub/phi4:14b") == "trading-hub/phi4:14b"
    with pytest.raises(workers.WorkerError, match="local Ollama"):
        workers.opencode_model("anthropic/claude-sonnet")
    assert "opencode" in policy.LOCAL_WORKERS


def test_agy_and_opencode_output_parsing():
    agy = "\n".join(json.dumps(event) for event in [
        {"event": "init", "conversation_id": "c"},
        {"event": "step_update", "step_update": {
            "step_type": "agent_response", "state": "ACTIVE", "text_delta": "ok"}},
        {"event": "result", "result": {"status": "SUCCESS", "response": "ok\n",
                                        "usage": {"input_tokens": 10, "output_tokens": 2}}},
    ])
    assert workers.extract_text(agy) == "ok\n"
    assert workers._extract_usage(agy) == {"input_tokens": 10, "output_tokens": 2}
    assert workers.humanize("agy", agy.splitlines()[1]) == "ok"
    opencode = "\n".join(json.dumps(event) for event in [
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "text", "part": {"type": "text", "text": "done"}},
        {"type": "step_finish", "part": {"type": "step-finish",
                                         "tokens": {"input": 100, "output": 5, "reasoning": 1}}},
        {"type": "step_finish", "part": {"type": "step-finish",
                                         "tokens": {"input": 50, "output": 5, "reasoning": 0}}},
    ])
    assert workers.extract_text(opencode) == "done"
    assert workers._extract_usage(opencode) == {
        "input_tokens": 150, "output_tokens": 10, "reasoning_tokens": 1,
    }


# ------------------------------------------------------ unattended starts ---
def _assigned(conn, project, worker, title="Review the plan"):
    tid = store.create_task(
        conn, project_id=project["id"], title=title, type="review",
        complexity=5, assignee=worker,
    )
    store.update_task(conn, tid, status="assigned")
    return tid


@pytest.fixture
def ready_workers(monkeypatch):
    monkeypatch.setattr(
        workers, "probe", lambda worker, max_age=300: {"availability": "ready", "note": None}
    )


def test_keep_working_skips_protected_and_quota_held(conn, git_repo, tmp_path, ready_workers):
    trading = _project(conn, "Trading Capability Hub", str(git_repo), program="trading-systems")
    held = _assigned(conn, trading, "claude")
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    ordinary = _project(conn, "Notes", str(other_repo))
    codex_task = _assigned(conn, ordinary, "codex")
    claude_task = _assigned(conn, ordinary, "claude")
    capacity.record(conn, [capacity.Reading(
        provider="codex", window="seven_day", used_percent=79.0,
        resets_at=_iso(timedelta(days=2)),
    )])
    skipped: dict[str, str] = {}
    selected = team.safe_start_candidates(conn, limit=3, skipped=skipped)
    assert selected == [claude_task]
    assert "excluded from unattended work" in skipped[held]
    assert "79% used" in skipped[codex_task]

    autonomy.set_paused(conn, True)
    paused: dict[str, str] = {}
    assert team.safe_start_candidates(conn, skipped=paused) == []
    assert "paused" in paused["*"]


def test_scheduler_dispatch_refuses_protected_project(conn, git_repo):
    apollo = _project(conn, "apollo-ats", str(git_repo))
    task = store.get_task(conn, _assigned(conn, apollo, "claude"))
    with pytest.raises(dispatcher.DispatchError, match="excluded"):
        dispatcher.dispatch(conn, task, started_by=dispatcher.SCHEDULER)


def test_scheduler_run_is_attributed_and_records_quota(conn, project, monkeypatch):
    task = store.get_task(conn, _assigned(conn, project, "claude"))
    output = "\n".join([
        json.dumps(CLAUDE_EVENT),
        json.dumps({"type": "result", "is_error": False, "result": "looks good"}),
    ])
    monkeypatch.setattr(workers, "execute", lambda *a, **k: workers.WorkerResult(0, output, "", None))
    result = dispatcher.dispatch(conn, task, started_by=dispatcher.SCHEDULER)
    run = store.get_run(conn, result.run_id)
    assert run["started_by"] == "scheduler"
    events = [
        row for row in store.list_activity_events(conn, project["id"], task_id=task["id"])
        if row["action"] == "run.unattended_start"
    ]
    assert len(events) == 1
    assert "allowing one run" in json.loads(events[0]["evidence_json"])["reason"]
    assert {w["window"] for w in capacity.current_windows(conn, "claude")} == {
        "five_hour", "seven_day"
    }
