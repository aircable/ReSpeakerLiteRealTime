import json

from gateway.db import Database


def test_project_session_transcript_and_atomic_plan_revision(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    project = db.get_project()
    session_id = db.start_session(project["id"], "test-device", "test-model")
    db.add_turn(session_id, "user", "We should choose option A.", "user-1")
    db.add_turn(session_id, "assistant", "What makes A preferable?", "assistant-1")

    assert [turn["role"] for turn in db.session_turns(session_id)] == ["user", "assistant"]
    update = {
        "summary": "Option A is under consideration.",
        "decisions": [],
        "open_questions": ["Why is A preferable?"],
        "plan_markdown": "## Plan\n\n- [ ] Compare A and B",
    }
    db.apply_plan_update(project["id"], session_id, update)

    saved = db.get_project(project["id"])
    history = db.plan_history(project["id"])
    assert saved["summary"] == update["summary"]
    assert json.loads(saved["open_questions_json"]) == update["open_questions"]
    assert history[0]["plan_markdown"] == update["plan_markdown"]


def test_only_one_project_can_be_active(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    second = db.create_project("Second")
    db.activate_project(second["id"])
    assert db.get_project()["id"] == second["id"]
    assert sum(project["active"] for project in db.list_projects()) == 1

