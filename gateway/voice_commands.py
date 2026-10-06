"""Voice commands offered to Realtime and dispatched by the device session.

Example phrases guide intent-based tool selection; they are not an exact-match
speech recognizer. Add a command here and implement its named session handler.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


EMPTY_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass(frozen=True)
class VoiceCommand:
    name: str
    handler: str
    purpose: str
    examples: tuple[str, ...]
    use_when: str
    avoid_when: str
    effect: str
    parameters: dict[str, Any]
    required_capability: str | None = None
    call_id_required: bool = True
    response_rule: str = ""
    unavailable_result: dict[str, Any] | None = None

    def available(self, capabilities: Mapping[str, bool]) -> bool:
        return self.required_capability is None or bool(
            capabilities.get(self.required_capability)
        )

    def tool_spec(self) -> dict[str, Any]:
        examples = "; ".join(f'"{phrase}"' for phrase in self.examples)
        return {
            "type": "function",
            "name": self.name,
            "description": (
                f"{self.purpose} Use when: {self.use_when} "
                f"Do not use when: {self.avoid_when} "
                f"Example user requests: {examples}. Effect: {self.effect}"
            ),
            "parameters": deepcopy(self.parameters),
        }

    def instruction(self) -> str:
        examples = "; ".join(f'"{phrase}"' for phrase in self.examples)
        return (
            f"- {self.name}: Call when {self.use_when} Examples: {examples}. "
            f"Do not call when {self.avoid_when} {self.response_rule}"
        )


VOICE_COMMANDS = (
    VoiceCommand(
        name="end_session",
        handler="_voice_end_session",
        purpose="End the device voice session and return to wake-word listening.",
        examples=("go to sleep", "end the session", "goodbye", "that's all"),
        use_when="the user clearly asks to end the entire voice session.",
        avoid_when=(
            "the user only wants to stop the current answer, pause, or cancel a task; "
            "an ambiguous 'stop' during playback is not an explicit session-ending request."
        ),
        effect="Ends the cloud session; the device returns to wake-word listening.",
        parameters=EMPTY_ARGUMENTS,
        call_id_required=False,
        response_rule="Acknowledge briefly before ending the session.",
    ),
    VoiceCommand(
        name="list_projects",
        handler="_voice_list_projects",
        purpose="List available projects and identify the active project without changing it.",
        examples=("what projects do I have", "which project is active"),
        use_when="the user asks which projects exist or which one is active.",
        avoid_when="the user asks to change projects rather than list them.",
        effect="Read-only lookup; no device or project state changes.",
        parameters=EMPTY_ARGUMENTS,
    ),
    VoiceCommand(
        name="switch_project",
        handler="_voice_switch_project",
        purpose="Switch to another existing project and open its separate voice context.",
        examples=("switch to Cooking Companion", "make First project active"),
        use_when="the user explicitly requests a change to a named project.",
        avoid_when="the user merely mentions, compares, or asks about a project.",
        effect=(
            "Changes the active project and opens a new Realtime session on the same device socket."
        ),
        parameters={
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "The project name as spoken by the user.",
                }
            },
            "required": ["project_name"],
            "additionalProperties": False,
        },
        response_rule="Do not claim the project changed until the command succeeds.",
    ),
    VoiceCommand(
        name="control_volume",
        handler="_voice_control_volume",
        purpose="Read or change the ReSpeaker playback volume.",
        examples=("set volume to 40 percent", "quieter", "what is the volume"),
        use_when="the user asks to report, set, raise, or lower this device's volume.",
        avoid_when="the user is discussing volume in another context, not controlling this device.",
        effect="A change is sent to and persisted by the device; get is read-only.",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "increase", "decrease"]},
                "level_percent": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Exact output level for the set action.",
                },
                "change_percent": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Optional relative change; defaults to 5 percentage points.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        response_rule="After a change, confirm briefly.",
    ),
    VoiceCommand(
        name="search_web",
        handler="_voice_search_web",
        purpose="Search the live web and return a short sourced answer.",
        examples=("search the web for today's weather", "look up the latest news about XMOS"),
        use_when="the user asks to search or needs current information beyond your knowledge.",
        avoid_when="the request can be answered from the current conversation or project context.",
        effect="Runs a paid OpenAI web search; the web UI displays clickable source citations.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The specific web search question, retaining key names and context.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        response_rule="Summarize the result briefly in speech; do not read URLs aloud.",
    ),
    VoiceCommand(
        name="wait_for_ready",
        handler="_voice_wait_for_ready",
        purpose=(
            "Pause microphone upload and wait for the device's local 'ready' detector; "
            "ordinary speech is ignored until it detects 'ready'."
        ),
        examples=("pause until ready", "wait for ready", "give me one step at a time"),
        use_when=(
            "the user explicitly asks to pause until the ready word, or the user requested a "
            "hands-free step-by-step procedure and you have just given one concise step."
        ),
        avoid_when="the user says only a casual 'wait a moment' or asks an ordinary question.",
        effect="Pauses microphone transport and arms the firmware's local ready-word model.",
        parameters=EMPTY_ARGUMENTS,
        required_capability="ready_keyword",
        response_rule=(
            "Do not say the user is ready before the tool returns. While it is pending, "
            "ordinary speech is ignored; the physical USER button can still end the session."
        ),
        unavailable_result={"ready": False, "error": "The device has no ready-word model."},
    ),
)

COMMANDS_BY_NAME = {command.name: command for command in VOICE_COMMANDS}
if len(COMMANDS_BY_NAME) != len(VOICE_COMMANDS):
    raise ValueError("Duplicate voice command name")


def available_commands(capabilities: Mapping[str, bool]) -> tuple[VoiceCommand, ...]:
    return tuple(command for command in VOICE_COMMANDS if command.available(capabilities))


def tool_specs(capabilities: Mapping[str, bool]) -> list[dict[str, Any]]:
    return [command.tool_spec() for command in available_commands(capabilities)]


def command_instructions(capabilities: Mapping[str, bool]) -> str:
    commands = available_commands(capabilities)
    return "\n".join(
        (
            "Voice command rules: Call only tools offered in this session. The phrases below "
            "are examples of intent, not exact-match commands. Never claim an action completed "
            "before its tool succeeds. If a project instruction names an unavailable tool, "
            "say it is unavailable rather than pretending to use it.",
            *(command.instruction() for command in commands),
        )
    )
