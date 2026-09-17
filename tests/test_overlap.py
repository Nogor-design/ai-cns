"""Finding the project you already built before you build it again."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortex import overlap, store


@pytest.fixture
def portfolio(conn, tmp_path):
    """A handful of projects with deliberately different and similar subjects."""
    made = {}
    for name, goal, stack in (
        ("Decision Garden", "A local decision register for small teams", "React"),
        ("Invoice Ledger", "Reconcile bank exports against supplier invoices", "Python"),
        ("Market Data Store", "Keep NinjaTrader minute and tick exports fresh", "Python"),
        ("Recipe Box", "Plan weekly meals and print a grocery list", "Svelte"),
    ):
        repo = tmp_path / name.lower().replace(" ", "-")
        repo.mkdir()
        made[name] = store.create_project(
            conn, name=name, repo_path=str(repo), current_goal=goal, stack=stack
        )
    return made


def test_stemming_folds_the_endings_that_cost_real_matches():
    assert overlap.stem("decisions") == "decision"
    assert overlap.stem("teams") == "team"
    assert overlap.stem("tracking") == "track"
    assert overlap.stem("libraries") == "library"
    # Never shortened below four characters, and never a false plural.
    assert overlap.stem("gas") == "gas"
    assert overlap.stem("analysis") == "analysis"
    assert overlap.stem("business") == "business"


def test_a_restated_idea_finds_the_project_that_already_does_it(conn, portfolio):
    result = overlap.search(
        conn, "somewhere to record the decisions a team makes", include_hub=False
    )
    assert result["matches"], "an obvious duplicate should be found"
    best = result["matches"][0]
    assert best["label"] == "Decision Garden"
    assert best["score"] >= overlap.DEFAULT_MIN_SCORE
    assert "decisions" in best["shared"] or "decision" in best["shared"]


def test_an_unrelated_idea_is_reported_as_unmatched(conn, portfolio):
    result = overlap.search(
        conn, "a bluetooth controller for stage lighting rigs", include_hub=False
    )
    assert result["matches"] == []
    assert result["considered"] == 4


def test_shared_terms_are_shown_as_words_people_wrote_not_stems(conn, portfolio):
    result = overlap.search(
        conn, "tracking the decisions my team makes", include_hub=False, min_score=0.0
    )
    shared = result["matches"][0]["shared"]
    assert "decisions" in shared
    assert "decision" not in shared  # the stem itself never reaches the screen


def test_a_project_lists_its_neighbours_and_never_itself(conn, portfolio):
    found = overlap.neighbours(
        conn, portfolio["Market Data Store"], include_hub=False, min_score=0.0
    )
    keys = [match["key"] for match in found]
    assert f"project:{portfolio['Market Data Store']}" not in keys
    assert keys, "other projects should be ranked even when unrelated"


def test_the_hub_registry_is_read_as_data_and_a_missing_one_is_survivable(
    conn, portfolio, tmp_path, monkeypatch
):
    registry = tmp_path / "hub" / "registry"
    registry.mkdir(parents=True)
    (registry / "capabilities.json").write_text(json.dumps({
        "capabilities": [
            {
                "id": "market-data-store",
                "name": "Local futures market-data store",
                "category": "market-data",
                "owner_path": "D:\\MarketData",
                "description": "Minute, tick and daily NinjaTrader exports with provenance.",
            }
        ]
    }), encoding="utf-8")
    monkeypatch.setenv(overlap.HUB_ROOT_ENV, str(tmp_path / "hub"))
    result = overlap.search(conn, "store NinjaTrader tick exports")
    assert any(match["source"] == "hub" for match in result["matches"])

    # A Hub that is absent or broken degrades to the Cortex projects alone.
    monkeypatch.setenv(overlap.HUB_ROOT_ENV, str(tmp_path / "no-hub-here"))
    assert overlap.search(conn, "store NinjaTrader tick exports")["considered"] == 4
    (registry / "capabilities.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv(overlap.HUB_ROOT_ENV, str(tmp_path / "hub"))
    assert overlap.search(conn, "store NinjaTrader tick exports")["considered"] == 4


def test_pairs_reports_each_overlap_once_strongest_first(conn, tmp_path):
    for name, goal in (
        ("Notes One", "Capture meeting notes and decisions for a team"),
        ("Notes Two", "Capture meeting notes and decisions for a team"),
        ("Weather Radar", "Render doppler radar tiles for pilots"),
    ):
        repo = tmp_path / name.lower().replace(" ", "-")
        repo.mkdir()
        store.create_project(conn, name=name, repo_path=str(repo), current_goal=goal)
    found = overlap.pairs(conn, include_hub=False)
    assert len(found) == 1
    labels = {found[0]["left"]["label"], found[0]["right"]["label"]}
    assert labels == {"Notes One", "Notes Two"}
    assert found[0]["score"] > overlap.DEFAULT_MIN_SCORE


def test_every_project_gets_neighbours_from_one_corpus_build(conn, portfolio):
    everyone = overlap.all_neighbours(conn, include_hub=False, min_score=0.0)
    assert set(everyone) == set(portfolio.values())
    for project_id, matches in everyone.items():
        assert all(match["project_id"] != project_id for match in matches)


def test_a_single_shared_word_is_a_coincidence_not_an_overlap(conn, tmp_path):
    """The real portfolio's top-scoring pair was a naming coincidence.

    seagate-demo and "Imported trading-agent demos" shared only the word
    "demos" and scored higher than every genuine match. Requiring two shared
    terms removes that class of false positive without losing the true ones.
    """
    for name, goal in (
        ("Seagate Demo", "A bounded synthetic factory-move replanning demo"),
        ("Kraken Demos", "Demos of on-chain trading agents and risk gates"),
    ):
        repo = tmp_path / name.lower().replace(" ", "-")
        repo.mkdir()
        store.create_project(conn, name=name, repo_path=str(repo), current_goal=goal)
    assert overlap.pairs(conn, include_hub=False) == []
    # The similarity is real; it is the evidence for it that is too thin.
    loose = overlap.pairs(conn, include_hub=False, min_score=0.0)
    assert loose == []
