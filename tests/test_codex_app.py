"""Safe Codex launch prompt and capability behavior."""

from __future__ import annotations

from cortex import codex_app, store


def test_launch_prompt_is_bounded_by_registered_project_and_task(conn, project):
    task_id = store.create_task(
        conn,
        project_id=project["id"],
        title="Build the safe bridge",
        brief="Implement preview and capability detection only.",
        acceptance="The owner can copy a complete prompt.",
        allowed_paths="cortex/**,dashboard/src/**",
        risk="medium",
        next_action="Add the preview endpoint",
    )
    prompt = codex_app.launch_prompt(project, store.get_task(conn, task_id))
    assert task_id in prompt
    assert str(project["repo_path"]) in prompt
    assert project["id"] in prompt
    assert "The owner can copy a complete prompt." in prompt
    assert "cortex/**,dashboard/src/**" in prompt
    assert "do not merge" in prompt
    assert "do not create a duplicate" in prompt


def test_inspect_reports_missing_cli_without_starting_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_app.shutil, "which", lambda _name: None)
    result = codex_app.inspect(tmp_path)
    assert result == {
        "available": False,
        "reason": "Codex CLI is not installed",
        "methods": {},
        "threads": [],
        "linked_thread_found": False,
    }


def test_inspect_is_read_only_and_scoped_to_exact_repository(monkeypatch, tmp_path):
    calls = []

    class FakeClient:
        def __init__(self):
            pass

        def request(self, method, params):
            calls.append((method, params))
            if method == "initialize":
                return {"userAgent": "Codex test"}
            assert method == "thread/list"
            return {
                "data": [
                    {
                        "id": "linked-thread",
                        "name": "Existing task",
                        "status": "idle",
                        "cwd": str(tmp_path.resolve()),
                    }
                ]
            }

        def notify(self, method, params=None):
            calls.append((method, params or {}))

        def close(self):
            calls.append(("close", {}))

    monkeypatch.setattr(codex_app.shutil, "which", lambda _name: "codex")
    monkeypatch.setattr(codex_app, "_supported_methods", lambda: {"thread/list"})
    monkeypatch.setattr(codex_app, "Client", FakeClient)

    result = codex_app._inspect_uncached(str(tmp_path), "linked-thread")

    assert result["linked_thread_found"] is True
    assert [method for method, _ in calls] == [
        "initialize",
        "initialized",
        "thread/list",
        "close",
    ]
    list_params = calls[2][1]
    assert list_params["cwd"] == str(tmp_path.resolve())
    assert list_params["useStateDbOnly"] is True
    assert "appServer" in list_params["sourceKinds"]
