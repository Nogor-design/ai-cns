"""Skill cards: the gate's contract, told to every worker in its own idiom."""

from __future__ import annotations

from cortex import dispatcher, skills, store, verification


def test_a_write_task_is_told_what_the_gate_checks():
    card = skills.render(
        "implement", worker="codex", test_command="pytest -q", allowed_paths='["src/**"]'
    )

    assert "acceptance criteria" in card
    assert "secrets in the diff" in card
    assert "`src/**`" in card
    assert "`pytest -q`" in card
    # The lesson a real run taught, stated before the agent can repeat it.
    assert "__pycache__" in card


def test_a_local_model_gets_the_same_rules_in_fewer_tokens():
    full = skills.render("implement", worker="codex", test_command="pytest -q")
    compact = skills.render("implement", worker="ollama", test_command="pytest -q")

    assert len(compact) < len(full) / 2
    for expected in ("allowed paths", "protected", "secrets", "tests"):
        assert expected in compact.lower()
    assert "###" not in compact


def test_a_reviewer_is_told_not_to_fix_anything():
    card = skills.render("review", worker="claude")

    assert "not improving it" in card
    assert "Do not edit any" in card
    # A reviewer has no allowed paths or test command to be told about.
    assert "allowed paths for this task" not in card


def test_the_protected_list_comes_from_the_gate_itself():
    """A card that promises what the gate does not check is worse than none."""
    note = skills.protected_note()

    for pattern, _ in verification.PROTECTED_FILES:
        assert f"`{pattern}`" in note


def test_the_card_reaches_the_worker_through_the_brief(conn, project):
    task_id = store.create_task(
        conn, project_id=project["id"], title="Add the parser", type="code",
        complexity=5, allowed_paths='["src/**"]',
    )

    preview = dispatcher.preview(conn, store.get_task(conn, task_id))

    assert "How Cortex works with you" in preview.brief or "RULES" in preview.brief
    assert "acceptance criterion" in preview.brief.lower()


def test_the_cards_can_be_read_by_the_owner():
    payload = skills.payload()

    assert {card["name"] for card in payload["cards"]} >= {
        "Finishing", "Reviewing, not fixing"
    }
    assert payload["by_action"]["implement"][0] == "How this change will be judged"
    assert "ollama" in payload["compact_workers"]
