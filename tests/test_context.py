import json

from gateway.context import build_instructions, extract_response_json


def test_context_contains_project_memory_and_recent_turns():
    project = {
        "name": "Launch",
        "goal": "Ship safely",
        "instructions": "Ask about risks",
        "pinned_notes": "Friday deadline",
        "summary": "Prototype works",
        "plan_markdown": "- [ ] Test",
    }
    text = build_instructions(project, [{"role": "user", "text": "Focus on reliability"}])
    assert all(value in text for value in ["Ship safely", "Friday deadline", "Prototype works", "Focus on reliability"])


def test_extract_structured_planner_output():
    expected = {"summary": "s", "decisions": [], "open_questions": [], "plan_markdown": "# Plan"}
    response = {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(expected)}]}]
    }
    assert extract_response_json(response) == expected

