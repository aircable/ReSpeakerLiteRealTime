import json

import pytest

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


def test_project_creation_saves_persona_fields_and_rejects_duplicate_names(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    project = db.create_project(
        "  Electronics Mentor  ", "Learn PCB design", "Explain tradeoffs clearly", "ESP32-S3 board"
    )
    assert project["name"] == "Electronics Mentor"
    assert project["goal"] == "Learn PCB design"
    assert project["instructions"] == "Explain tradeoffs clearly"
    assert project["pinned_notes"] == "ESP32-S3 board"
    assert project["active"] == 0
    with pytest.raises(ValueError, match="already exists"):
        db.create_project("electronics  MENTOR")


def test_find_project_accepts_case_insensitive_unique_spoken_match(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    second = db.create_project("Garden Irrigation")

    assert db.find_project("  GARDEN irrigation  ")["id"] == second["id"]
    assert db.find_project("Irrigation")["id"] == second["id"]
    assert db.find_project("missing") is None


def test_project_turns_can_be_filtered_to_one_device(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    project_id = db.get_project()["id"]
    first = db.start_session(project_id, "kitchen", "test-model")
    second = db.start_session(project_id, "office", "test-model")
    db.add_turn(first, "user", "Kitchen question")
    db.add_turn(second, "user", "Office question")

    assert [turn["text"] for turn in db.project_turns(project_id)] == [
        "Kitchen question", "Office question"
    ]
    assert [turn["text"] for turn in db.project_turns(project_id, device_id="office")] == [
        "Office question"
    ]


def test_device_profiles_keep_room_settings_and_last_volume_independent(tmp_path):
    path = tmp_path / "profiles.db"
    db = Database(path)
    db.initialize()
    db.register_device("0D44", "Kitchen Companion")
    db.register_device("AB12", "Desk Companion")
    db.update_device_settings("0D44", {"listening_rms_threshold": 300, "xvf_agc_ch0_gain": 30})
    db.update_device_volume("0D44", 48)

    reopened = Database(path)
    assert reopened.device_setting_overrides("0D44") == {
        "listening_rms_threshold": 300, "xvf_agc_ch0_gain": 30,
    }
    assert reopened.device_setting_overrides("AB12") == {}
    profiles = {row["device_id"]: row for row in reopened.list_device_profiles()}
    assert profiles["0D44"]["volume_percent"] == 48
    assert profiles["AB12"]["volume_percent"] is None


def test_timers_persist_and_ring_only_on_the_owning_device(tmp_path):
    path = tmp_path / "timers.db"
    db = Database(path)
    db.initialize()
    kitchen = db.create_timer("kitchen", 60, "pasta")
    office = db.create_timer("office", 60, "meeting")
    assert [row["id"] for row in Database(path).list_timers("kitchen")] == [kitchen["id"]]
    with db.connect() as conn:
        conn.execute("UPDATE timers SET due_at='2000-01-01T00:00:00+00:00'")

    assert set(db.fire_due_timers()) == {"kitchen", "office"}
    assert db.fire_due_timers() == []
    assert db.next_ringing_timer("kitchen")["id"] == kitchen["id"]
    assert db.next_ringing_timer("office")["id"] == office["id"]
    assert db.close_timer("office", kitchen["id"], dismissed=True) is None
    assert db.close_timer("kitchen", kitchen["id"], dismissed=True)["status"] == "dismissed"
    assert db.next_ringing_timer("kitchen") is None
