from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

Handler = Callable[[Any, dict], dict]  # (user: User, params: dict) -> dict

TOOL_SCOPES = ("list_teams", "list_members", "find_slots", "check_time")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str                       # e.g. "slotly.list_teams"
    description: str
    input_schema: dict              # JSON Schema (object) for params
    handler: Handler


_REGISTRY: dict[str, ToolDefinition] = {}


def register_tool(
    name: str,
    *,
    description: str,
    input_schema: dict,
) -> Callable[[Handler], Handler]:
    def _decorator(func: Handler) -> Handler:
        if name in _REGISTRY:
            raise ValueError(f"Tool {name!r} already registered")
        _REGISTRY[name] = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=func,
        )
        return func
    return _decorator


def get_tool(name: str) -> ToolDefinition | None:
    return _REGISTRY.get(name)


def list_tools() -> list[ToolDefinition]:
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


@register_tool(
    "slotly.list_teams",
    description="Vrátí seznam týmů, ve kterých je uživatel členem.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def list_teams(user, params):
    return {"teams": []}  # dummy — T4 replaces


@register_tool(
    "slotly.list_members",
    description="Vrátí členy daného týmu.",
    input_schema={
        "type": "object",
        "properties": {"team_id": {"type": "integer"}},
        "required": ["team_id"],
        "additionalProperties": False,
    },
)
def list_members(user, params):
    return {"members": []}  # dummy — T4 replaces


@register_tool(
    "slotly.find_slots",
    description="Najdi společné volné sloty pro členy týmu v zadaném okně.",
    input_schema={
        "type": "object",
        "properties": {
            "team_id": {"type": "integer"},
            "member_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "duration_min": {"type": "integer", "minimum": 15, "maximum": 1440},
            "window_start": {"type": "string", "format": "date-time"},
            "window_end": {"type": "string", "format": "date-time"},
            "buffer_min": {"type": "integer", "minimum": 0, "maximum": 240, "default": 0},
        },
        "required": ["team_id", "member_ids", "duration_min"],
        "additionalProperties": False,
    },
)
def find_slots(user, params):
    return {"slots": [], "count": 0, "truncated": False}  # dummy — T5 replaces


@register_tool(
    "slotly.check_time",
    description="Ověř zda jsou vybraní členové týmu volní v daném okně.",
    input_schema={
        "type": "object",
        "properties": {
            "team_id": {"type": "integer"},
            "member_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "start": {"type": "string", "format": "date-time"},
            "end": {"type": "string", "format": "date-time"},
        },
        "required": ["team_id", "member_ids", "start", "end"],
        "additionalProperties": False,
    },
)
def check_time(user, params):
    return {"everyone_free": True, "people": []}  # dummy — T6 replaces
