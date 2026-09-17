"""Owner-chosen cloud model and reasoning level per worker."""

from __future__ import annotations

import json

import pytest

from cortex import dispatcher, lanes, model_catalog, routing, store, workers


@pytest.fixture
def codex_cache(tmp_path, monkeypatch):
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps({"models": [
        {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list",
         "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": e} for e in
                                        ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list",
         "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")]},
        {"slug": "codex-auto-review", "display_name": "Codex Auto Review", "visibility": "hide",
         "supported_reasoning_levels": [{"effort": "low"}]},
    ]}), encoding="utf-8")
    monkeypatch.setenv(model_catalog.CODEX_MODELS_ENV, str(path))
    return path


def test_catalog_reads_codex_cache_and_hides_internal_models(codex_cache):
    slugs = [entry["slug"] for entry in model_catalog.codex_models()]
    assert slugs == ["gpt-6-astra", "gpt-5.6-luna"]
    assert model_catalog.efforts_for("codex", "gpt-5.6-luna") == ("low", "medium", "high")
    assert "ultra" in model_catalog.efforts_for("codex", "gpt-6-astra")


def test_catalog_falls_back_when_the_cache_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(model_catalog.CODEX_MODELS_ENV, str(tmp_path / "absent.json"))
    assert [entry["slug"] for entry in model_catalog.codex_models()]
    assert {entry["slug"] for entry in model_catalog.models_for("claude")} >= {"opus", "sonnet"}


def test_choices_are_validated(conn, codex_cache):
    assert model_catalog.choice(conn, "codex") == {"model": "auto", "effort": "auto"}
    model_catalog.set_choice(conn, "codex", model="gpt-6-astra", effort="ultra")
    assert model_catalog.choice(conn, "codex") == {"model": "gpt-6-astra", "effort": "ultra"}
    with pytest.raises(ValueError):
        model_catalog.set_choice(conn, "codex", model="gpt-4-imaginary")
    with pytest.raises(ValueError):
        model_catalog.set_choice(conn, "codex", model="gpt-5.6-luna", effort="ultra")
    with pytest.raises(ValueError):
        model_catalog.set_choice(conn, "grok", model="anything")


def test_owner_choice_overrides_routing_but_not_the_task(conn, project, codex_cache):
    model_catalog.set_choice(conn, "codex", model="gpt-6-astra", effort="high")
    task_id = store.create_task(conn, project_id=project["id"], type="code",
                                title="Refactor the migration runner", risk="high")
    planned = dispatcher.preview(conn, store.get_task(conn, task_id))
    assert planned.route.worker == "codex"
    assert (planned.route.model, planned.route.effort) == ("gpt-6-astra", "high")
    assert "--model" in planned.command.argv
    assert 'model_reasoning_effort="high"' in planned.command.argv

    # A task that names its own model or level still wins.
    store.update_task(conn, task_id, requested_model="gpt-5.6-luna", effort="low")
    planned = dispatcher.preview(conn, store.get_task(conn, task_id))
    assert (planned.route.model, planned.route.effort) == ("gpt-5.6-luna", "low")


def test_unsupported_level_falls_back_to_the_models_best(conn, project, codex_cache):
    model_catalog.set_choice(conn, "codex", model="gpt-6-astra", effort="ultra")
    model_catalog.set_choice(conn, "codex", model="gpt-5.6-luna")
    task_id = store.create_task(conn, project_id=project["id"], type="code",
                                title="Rewrite the auth middleware", risk="high")
    planned = dispatcher.preview(conn, store.get_task(conn, task_id))
    # Luna has no 'ultra'; the CLI would reject it, so the highest it has is used.
    assert (planned.route.model, planned.route.effort) == ("gpt-5.6-luna", "high")


def test_local_lane_payload_hides_models_that_cannot_be_used(conn, monkeypatch):
    catalog = [
        {"name": "phi4:14b", "lane": "gpu", "size_gb": 9.1},
        {"name": "qwen2.5:72b", "lane": "avoid", "size_gb": 44.2},
        {"name": "nomic-embed-text:latest", "lane": "embedding", "size_gb": 0.3},
    ]
    monkeypatch.setattr(lanes, "models", lambda *a, **k: [dict(row) for row in catalog])
    monkeypatch.setattr(lanes, "hardware", lambda *a, **k: lanes.Hardware(
        gpu_name="RTX 3060", vram_mb=12288, vram_used_mb=0, gpu_util_pct=0, ram_mb=131072))
    monkeypatch.setattr(lanes, "loaded_models", lambda: [])
    monkeypatch.setattr(lanes, "ninjatrader_running", lambda *a, **k: False)

    payload = lanes.payload(conn)
    assert [row["name"] for row in payload["models"]] == ["phi4:14b"]
    assert {row["name"] for row in payload["hidden_models"]} == {
        "qwen2.5:72b", "nomic-embed-text:latest"
    }
    assert len(lanes.payload(conn, include_unusable=True)["models"]) == 3
    allowed, why = lanes.admit(conn, "qwen2.5:72b")
    assert not allowed and "avoid" in why
