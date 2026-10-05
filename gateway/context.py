import json
from typing import Any

from .voice_commands import command_instructions


BASE_INSTRUCTIONS = """You are a Socratic thinking companion. Help the user clarify goals,
surface assumptions, compare options, and decide concrete next actions. Be concise in speech."""


def build_instructions(
    project: dict[str, Any],
    turns: list[dict[str, Any]],
    ready_keyword_enabled: bool = False,
) -> str:
    history = "\n".join(f"{turn['role']}: {turn['text']}" for turn in turns)
    commands = command_instructions({"ready_keyword": ready_keyword_enabled})
    return f"""{BASE_INSTRUCTIONS}

{commands}

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
