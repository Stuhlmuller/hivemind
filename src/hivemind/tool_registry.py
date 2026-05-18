from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


TOOL_ACTION_RISK_LEVELS = {"low", "medium", "high"}
PAYLOAD_TYPE_MATCHERS: dict[str, Callable[[Any], bool]] = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
}
DEFAULT_TOOL_ACTIONS = (
    {
        "name": "read_repo",
        "description": "Read repository metadata or file context.",
        "required_credential_action": "read_repo",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
            "additionalProperties": True,
        },
    },
    {
        "name": "open_issue",
        "description": "Open an issue in a repository.",
        "required_credential_action": "open_issue",
        "risk_level": "high",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}},
            "required": ["repo", "title"],
            "additionalProperties": True,
        },
    },
    {
        "name": "open_feature_request",
        "description": "Open a feature request in a tracker.",
        "required_credential_action": "open_feature_request",
        "risk_level": "high",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": True,
        },
    },
    {
        "name": "issue_installation_token",
        "description": "Issue a short-lived GitHub App installation token.",
        "required_credential_action": "issue_installation_token",
        "risk_level": "medium",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    },
    {
        "name": "exchange_oauth_code",
        "description": "Exchange an OAuth authorization code through the broker.",
        "required_credential_action": "exchange_oauth_code",
        "risk_level": "medium",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    },
    {
        "name": "refresh_oauth_token",
        "description": "Refresh an OAuth credential through the broker.",
        "required_credential_action": "refresh_oauth_token",
        "risk_level": "medium",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    },
    {
        "name": "delegate_code",
        "description": "Delegate a bounded coding task through an agent provider.",
        "required_credential_action": "delegate_code",
        "risk_level": "medium",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    },
    {
        "name": "review_code",
        "description": "Request a code review through an agent provider.",
        "required_credential_action": "review_code",
        "risk_level": "medium",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    },
)


def normalize_tool_action_name(name: str) -> str:
    return name.strip().lower()


def validate_tool_action_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        raise ValueError("tool action input_schema must be a JSON object")
    normalized = dict(schema)
    if normalized.get("type", "object") != "object":
        raise ValueError("tool action input_schema type must be object")
    properties = normalized.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("tool action input_schema properties must be an object")
    required = normalized.get("required", [])
    if not isinstance(required, list) or any(not isinstance(field, str) or not field for field in required):
        raise ValueError("tool action input_schema required must be a list of field names")
    missing_required = sorted(set(required) - set(properties))
    if missing_required:
        raise ValueError(
            f"tool action input_schema required field is not declared in properties: {missing_required[0]}"
        )
    for field, field_schema in properties.items():
        if not isinstance(field_schema, Mapping):
            raise ValueError(f"tool action input_schema property must be an object: {field}")
        expected_type = field_schema.get("type")
        if isinstance(expected_type, str) and expected_type not in PAYLOAD_TYPE_MATCHERS:
            raise ValueError(f"tool action input_schema property type is unsupported: {field}")
    additional_properties = normalized.get("additionalProperties", True)
    if not isinstance(additional_properties, bool):
        raise ValueError("tool action input_schema additionalProperties must be boolean")
    normalized["properties"] = dict(properties)
    normalized["required"] = required
    normalized["additionalProperties"] = additional_properties
    return normalized


def payload_type_matches(value: Any, expected_type: str) -> bool:
    matcher = PAYLOAD_TYPE_MATCHERS.get(expected_type)
    return bool(matcher and matcher(value))


def payload_required_error(required: Any, payload: Mapping[str, Any]) -> str | None:
    for field in required:
        if field not in payload:
            return f"payload missing required field: {field}"
    return None


def payload_property_error(field: str, field_schema: Any, payload: Mapping[str, Any]) -> str | None:
    if field not in payload or not isinstance(field_schema, Mapping):
        return None
    expected_type = field_schema.get("type")
    if not isinstance(expected_type, str):
        return None
    if expected_type not in PAYLOAD_TYPE_MATCHERS:
        return f"payload field {field} uses unsupported type: {expected_type}"
    if not payload_type_matches(payload[field], expected_type):
        return f"payload field {field} must be {expected_type}"
    return None


def payload_properties_error(properties: Any, payload: Mapping[str, Any]) -> str | None:
    for field, field_schema in properties.items():
        error = payload_property_error(field, field_schema, payload)
        if error:
            return error
    return None


def payload_additional_properties_error(schema: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    if schema.get("additionalProperties", True) is not False:
        return None
    properties = schema.get("properties", {})
    allowed_fields = set(properties) if isinstance(properties, Mapping) else set()
    extra_fields = sorted(set(payload) - allowed_fields)
    if extra_fields:
        return f"payload includes unknown field: {extra_fields[0]}"
    return None


def payload_schema_error(schema: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    required_error = payload_required_error(required, payload)
    if required_error:
        return required_error
    if isinstance(properties, Mapping):
        properties_error = payload_properties_error(properties, payload)
        if properties_error:
            return properties_error
    return payload_additional_properties_error(schema, payload)
