import json
from typing import Any


BASE_INSTRUCTIONS = """You are a Socratic thinking companion. Help the user clarify goals,
surface assumptions, compare options, and decide concrete next actions. Be concise in speech.
Do not pretend an action was completed. When the user says "go to sleep", stop, goodbye, end
session, good night, "that's all", or an equivalent explicit command, acknowledge briefly and
call the end_session tool. Use list_projects when the user asks which projects are available. Use
switch_project only when the user explicitly asks to change the active project. Never claim a
project was switched until the tool succeeds. Use control_volume for requests to set, raise,
lower, or report the device volume. Give only a brief confirmation after a volume change."""

READY_KEYWORD_INSTRUCTIONS = """The device has a local 'ready' detector. In a hands-free,
step-by-step procedure, give exactly one concise step and then call wait_for_ready. Do not say
that the user is ready before the tool returns. While that tool is pending, ordinary speech is
intentionally ignored; the physical USER button can still end the session."""


def build_instructions(
    project: dict[str, Any],
    turns: list[dict[str, Any]],
    ready_keyword_enabled: bool = False,
) -> str:
    history = "\n".join(f"{turn['role']}: {turn['text']}" for turn in turns)
    ready_instructions = f"\n\n{READY_KEYWORD_INSTRUCTIONS}" if ready_keyword_enabled else ""
    return f"""{BASE_INSTRUCTIONS}{ready_instructions}

Project: {project['name']}
Goal: {project['goal']}
Project instructions: {project['instructions']}
Pinned notes: {project['pinned_notes']}
Rolling summary: {project['summary']}
Current plan:\n{project['plan_markdown']}

Recent conversation:\n{history or '(none)'}
""".strip()


def planner_input(project: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    transcript = "\n".join(f"{turn['role']}: {turn['text']}" for turn in turns)
    return f"""Update this project's durable memory after a voice session.
Preserve settled facts and completed tasks. Do not invent decisions. The Markdown plan should be
editable, concise, and use checkboxes for actions when appropriate.

Project goal: {project['goal']}
Previous summary: {project['summary']}
Previous decisions: {project['decisions_json']}
Previous open questions: {project['open_questions_json']}
Previous plan:\n{project['plan_markdown']}

Session transcript:\n{transcript}
"""


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "plan_markdown": {"type": "string"},
    },
    "required": ["summary", "decisions", "open_questions", "plan_markdown"],
    "additionalProperties": False,
}


def extract_response_json(response: dict[str, Any]) -> dict[str, Any]:
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                return json.loads(content["text"])
    raise ValueError("planner response contained no output_text")
