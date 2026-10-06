from gateway.context import build_instructions
from gateway.session import DeviceSession
from gateway.voice_commands import COMMANDS_BY_NAME, VOICE_COMMANDS, tool_specs


PROJECT = {
    "name": "First project",
    "goal": "Think clearly",
    "instructions": "Be concise",
    "pinned_notes": "",
    "summary": "",
    "plan_markdown": "",
}


def test_registry_names_and_handlers_are_unique_and_implemented():
    assert len(COMMANDS_BY_NAME) == len(VOICE_COMMANDS)
    assert len({command.handler for command in VOICE_COMMANDS}) == len(VOICE_COMMANDS)
    for command in VOICE_COMMANDS:
        assert callable(getattr(DeviceSession, command.handler, None))
        assert command.examples
        assert command.effect


def test_ready_tool_is_advertised_only_with_device_capability():
    without_ready = tool_specs({"ready_keyword": False})
    with_ready = tool_specs({"ready_keyword": True})

    assert {tool["name"] for tool in without_ready} == {
        "end_session", "list_projects", "switch_project", "control_volume", "search_web"
    }
    assert {tool["name"] for tool in with_ready} == {
        tool["name"] for tool in without_ready
    } | {"wait_for_ready"}
    assert "pause until ready" in next(
        tool["description"] for tool in with_ready if tool["name"] == "wait_for_ready"
    )


def test_context_rules_follow_same_capability_gate_as_tool_list():
    without_ready = build_instructions(PROJECT, [], ready_keyword_enabled=False)
    with_ready = build_instructions(PROJECT, [], ready_keyword_enabled=True)

    assert "wait_for_ready" not in without_ready
    assert "pause until ready" not in without_ready
    assert "wait_for_ready" in with_ready
    assert "pause until ready" in with_ready
    assert "casual 'wait a moment'" in with_ready
    assert "stop the current answer" in with_ready


def test_tool_schema_is_copied_from_registry():
    first = tool_specs({"ready_keyword": True})
    first[0]["parameters"]["properties"]["injected"] = {"type": "string"}

    second = tool_specs({"ready_keyword": True})
    assert "injected" not in second[0]["parameters"]["properties"]
