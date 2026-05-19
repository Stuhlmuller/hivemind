from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hivemind.declarative import import_declarative_config, validate_declarative_config
from hivemind.store import HivemindStore, StoreError, StoreNotFoundError, StoreValidationError, dumps, iso
from hivemind.tool_registry import payload_schema_error, validate_tool_action_schema


QUEEN_BEE_AGENT_ID = "agent_queen_bee"
QUEEN_BEE_NAME = "Queen Bee"
QUEEN_BEE_TARGET_ID = "queen-bee"
QUEEN_BEE_SYSTEM_PROMPT = (
    "Operate Hivemind through first-party tools. Keep actions brief, auditable, "
    "and policy-scoped. Request JIT credential capabilities through the broker; "
    "never ask for raw secrets."
)

ToolHandler = Callable[[HivemindStore, dict[str, Any], str], Any]


@dataclass(frozen=True)
class OperatorTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_level: str
    mutates: bool
    handler: ToolHandler

    def public_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk_level": self.risk_level,
            "mutates": self.mutates,
        }


def _schema(
    properties: dict[str, dict[str, Any]] | None = None,
    *,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    return validate_tool_action_schema(
        {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": additional_properties,
        }
    )


def _read_public_config(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    return store.config.public_view()


def _read_runtime_overview(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    return store.runtime_overview()


def _list_hives(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> list[dict[str, Any]]:
    return store.list_hives()


def _list_agents(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> list[dict[str, Any]]:
    return store.list_agents()


def _list_credentials(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> list[dict[str, Any]]:
    return store.list_credentials()


def _list_tasks(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> list[dict[str, Any]]:
    return store.list_tasks()


def _list_schedules(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> list[dict[str, Any]]:
    return store.list_schedules()


def _list_audit_events(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> list[dict[str, Any]]:
    return store.list_audit_events()


def _require_non_empty_strings(arguments: Mapping[str, Any], *fields: str) -> None:
    for field in fields:
        if not str(arguments.get(field) or "").strip():
            raise StoreValidationError(f"{field} is required")


def _register_agent(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    _require_non_empty_strings(arguments, "name", "role")
    max_subagents = int(arguments.get("max_subagents") or 0)
    if max_subagents > 64:
        raise StoreValidationError("max_subagents must be 64 or less")
    return store.create_agent(arguments, actor_id=QUEEN_BEE_AGENT_ID)


def _set_agent_status(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    return store.update_agent_status(
        str(arguments["agent_id"]),
        str(arguments["status"]),
        actor_id=QUEEN_BEE_AGENT_ID,
    )


def _create_task(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    _require_non_empty_strings(arguments, "title")
    heartbeat_seconds = arguments.get("heartbeat_seconds")
    if heartbeat_seconds is not None and int(heartbeat_seconds) < 30:
        raise StoreValidationError("heartbeat_seconds must be at least 30")
    return store.create_task(arguments, actor_id=QUEEN_BEE_AGENT_ID)


def _update_task(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    changes = arguments["changes"]
    if not isinstance(changes, Mapping):
        raise StoreValidationError("changes must be an object")
    heartbeat_seconds = changes.get("heartbeat_seconds")
    if heartbeat_seconds is not None and int(heartbeat_seconds) < 30:
        raise StoreValidationError("heartbeat_seconds must be at least 30")
    return store.update_task(
        str(arguments["task_id"]),
        dict(changes),
        actor_id=QUEEN_BEE_AGENT_ID,
    )


def _create_schedule(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    _require_non_empty_strings(arguments, "name", "task_title")
    return store.create_schedule(arguments, actor_id=QUEEN_BEE_AGENT_ID)


def _set_schedule_enabled(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    return store.update_schedule_enabled(
        str(arguments["schedule_id"]),
        bool(arguments["enabled"]),
        actor_id=QUEEN_BEE_AGENT_ID,
    )


def _run_due_schedules(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    return {"created_tasks": store.run_due_schedules_once(actor_id=QUEEN_BEE_AGENT_ID)}


def _create_credential_reference(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    _require_non_empty_strings(arguments, "name", "provider", "secret_ref")
    scheme, _, _ = str(arguments["secret_ref"]).partition("://")
    if scheme in {"oauth", "secret"}:
        raise StoreValidationError("Queen Bee accepts only external env://, file://, or vault:// secret references")
    max_ttl_seconds = int(arguments.get("max_ttl_seconds") or 300)
    if max_ttl_seconds < 1 or max_ttl_seconds > 3600:
        raise StoreValidationError("max_ttl_seconds must be between 1 and 3600")
    return store.create_credential(arguments)


def _validate_declarative(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    return validate_declarative_config(store, arguments["config"])


def _import_declarative(store: HivemindStore, arguments: dict[str, Any], operator_id: str) -> dict[str, Any]:
    return import_declarative_config(
        store,
        arguments["config"],
        actor_id=QUEEN_BEE_AGENT_ID,
        dry_run=bool(arguments.get("dry_run", True)),
    )


OPERATOR_TOOLS: dict[str, OperatorTool] = {
    tool.name: tool
    for tool in (
        OperatorTool(
            "read_public_config",
            "Read redacted runtime configuration visible to authenticated operators.",
            _schema(),
            "low",
            False,
            _read_public_config,
        ),
        OperatorTool(
            "read_runtime_overview",
            "Read runtime counts, due schedules, stale heartbeats, and recent failed tasks.",
            _schema(),
            "low",
            False,
            _read_runtime_overview,
        ),
        OperatorTool("list_hives", "List project hives and their operational counts.", _schema(), "low", False, _list_hives),
        OperatorTool("list_agents", "List registered agents and assignment rollups.", _schema(), "low", False, _list_agents),
        OperatorTool("list_credentials", "List redacted credential policies and lease limits.", _schema(), "low", False, _list_credentials),
        OperatorTool("list_tasks", "List tasks, heartbeat state, credential intent, and assignments.", _schema(), "low", False, _list_tasks),
        OperatorTool("list_schedules", "List schedules and generated task templates.", _schema(), "low", False, _list_schedules),
        OperatorTool("list_audit_events", "List recent audit events for operator review.", _schema(), "low", False, _list_audit_events),
        OperatorTool(
            "register_agent",
            "Register a new agent using the same validation as the operator API.",
            _schema(
                {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "provider": {"type": "string"},
                    "model": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "hive_id": {"type": "string"},
                    "can_spawn_subagents": {"type": "boolean"},
                    "max_subagents": {"type": "integer"},
                    "issue_creation_enabled": {"type": "boolean"},
                    "issue_kind": {"type": "string"},
                    "issue_rate_limit_per_hour": {"type": "integer"},
                    "issue_labels": {"type": "array"},
                },
                required=["name", "role"],
            ),
            "medium",
            True,
            _register_agent,
        ),
        OperatorTool(
            "set_agent_status",
            "Update an agent lifecycle status.",
            _schema(
                {"agent_id": {"type": "string"}, "status": {"type": "string"}},
                required=["agent_id", "status"],
            ),
            "medium",
            True,
            _set_agent_status,
        ),
        OperatorTool(
            "create_task",
            "Create a task through the same assignment and credential policy checks as the operator API.",
            _schema(
                {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "priority": {"type": "string"},
                    "hive_id": {"type": "string"},
                    "assigned_agent_id": {"type": "string"},
                    "credential_id": {"type": "string"},
                    "action": {"type": "string"},
                    "intent": {"type": "string"},
                    "heartbeat_seconds": {"type": "integer"},
                },
                required=["title"],
            ),
            "medium",
            True,
            _create_task,
        ),
        OperatorTool(
            "update_task",
            "Update editable task fields through existing task validation.",
            _schema(
                {"task_id": {"type": "string"}, "changes": {"type": "object"}},
                required=["task_id", "changes"],
            ),
            "medium",
            True,
            _update_task,
        ),
        OperatorTool(
            "create_schedule",
            "Create a schedule through the same cadence and assignment checks as the operator API.",
            _schema(
                {
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "interval_seconds": {"type": "integer"},
                    "catch_up_policy": {"type": "string"},
                    "task_title": {"type": "string"},
                    "task_description": {"type": "string"},
                    "priority": {"type": "string"},
                    "hive_id": {"type": "string"},
                    "assigned_agent_id": {"type": "string"},
                    "credential_id": {"type": "string"},
                    "action": {"type": "string"},
                    "intent": {"type": "string"},
                    "next_run_at": {"type": "string"},
                },
                required=["name", "interval_seconds", "task_title"],
            ),
            "medium",
            True,
            _create_schedule,
        ),
        OperatorTool(
            "set_schedule_enabled",
            "Pause or resume a schedule.",
            _schema(
                {"schedule_id": {"type": "string"}, "enabled": {"type": "boolean"}},
                required=["schedule_id", "enabled"],
            ),
            "medium",
            True,
            _set_schedule_enabled,
        ),
        OperatorTool(
            "run_due_schedules",
            "Trigger due schedules once through the scheduler path.",
            _schema(),
            "medium",
            True,
            _run_due_schedules,
        ),
        OperatorTool(
            "create_credential_reference",
            "Create a credential policy from an external secret reference without accepting raw secret material.",
            _schema(
                {
                    "name": {"type": "string"},
                    "provider": {"type": "string"},
                    "secret_ref": {"type": "string"},
                    "allowed_agents": {"type": "array"},
                    "allowed_actions": {"type": "array"},
                    "approval_required_actions": {"type": "array"},
                    "max_ttl_seconds": {"type": "integer"},
                    "require_intent": {"type": "boolean"},
                    "agent_lease_limit": {"type": "integer"},
                    "credential_lease_limit": {"type": "integer"},
                    "credential_action_limit": {"type": "integer"},
                    "rate_limit_window_seconds": {"type": "integer"},
                    "provider_token_budget": {"type": "integer"},
                    "provider_cost_budget_cents": {"type": "integer"},
                    "metadata": {"type": "object"},
                },
                required=["name", "provider", "secret_ref", "allowed_actions"],
            ),
            "high",
            True,
            _create_credential_reference,
        ),
        OperatorTool(
            "validate_declarative_config",
            "Validate a declarative Hivemind config without applying it.",
            _schema({"config": {"type": "object"}}, required=["config"]),
            "low",
            False,
            _validate_declarative,
        ),
        OperatorTool(
            "import_declarative_config",
            "Apply or dry-run a declarative config containing agents, credential refs, and schedules.",
            _schema(
                {"config": {"type": "object"}, "dry_run": {"type": "boolean"}},
                required=["config"],
            ),
            "high",
            True,
            _import_declarative,
        ),
    )
}


def queen_bee_tools_manifest() -> list[dict[str, Any]]:
    return [tool.public_view() for tool in OPERATOR_TOOLS.values()]


def queen_bee_profile(store: HivemindStore) -> dict[str, Any]:
    try:
        agent = store.get_agent(QUEEN_BEE_AGENT_ID)
        provisioned = True
    except StoreNotFoundError:
        agent = {
            "id": QUEEN_BEE_AGENT_ID,
            "name": QUEEN_BEE_NAME,
            "role": "First-party operator/admin agent for Hivemind self-management.",
            "status": "not_provisioned",
            "provider": "local",
            "model": store.config.agent_provider("local").model,
        }
        provisioned = False
    return {
        "id": QUEEN_BEE_AGENT_ID,
        "name": QUEEN_BEE_NAME,
        "provisioned": provisioned,
        "agent": agent,
        "tools": queen_bee_tools_manifest(),
    }


def ensure_queen_bee_agent(store: HivemindStore, *, actor_id: str) -> dict[str, Any]:
    try:
        return store.get_agent(QUEEN_BEE_AGENT_ID)
    except StoreNotFoundError:
        pass

    now = iso()
    row = {
        "id": QUEEN_BEE_AGENT_ID,
        "name": QUEEN_BEE_NAME,
        "role": "First-party operator/admin agent for Hivemind self-management.",
        "provider": "local",
        "model": store.config.agent_provider("local").model,
        "status": "idle",
        "system_prompt": QUEEN_BEE_SYSTEM_PROMPT,
        "hive_id": None,
        "can_spawn_subagents": 0,
        "max_subagents": 0,
        "issue_creation_enabled": 0,
        "issue_kind": "issue",
        "issue_rate_limit_per_hour": 0,
        "issue_labels": dumps([]),
        "created_at": now,
        "updated_at": now,
    }
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO agents
            (id, name, role, provider, model, status, system_prompt, hive_id,
             can_spawn_subagents, max_subagents, issue_creation_enabled, issue_kind,
             issue_rate_limit_per_hour, issue_labels, created_at, updated_at)
            VALUES
            (:id, :name, :role, :provider, :model, :status, :system_prompt, :hive_id,
             :can_spawn_subagents, :max_subagents, :issue_creation_enabled, :issue_kind,
             :issue_rate_limit_per_hour, :issue_labels, :created_at, :updated_at)
            """,
            row,
        )
        public_row = store.public_agent(conn, row)
    store.audit(
        "agent.created",
        actor_id,
        QUEEN_BEE_AGENT_ID,
        "allowed",
        "Queen Bee provisioned",
        {"status": "idle", "first_party_operator": True},
    )
    return public_row


def _queen_bee_actor(store: HivemindStore, operator_id: str) -> str:
    try:
        store.get_agent(QUEEN_BEE_AGENT_ID)
        return QUEEN_BEE_AGENT_ID
    except StoreNotFoundError:
        return operator_id


def _payload_key_count(arguments: Any) -> int:
    if isinstance(arguments, Mapping):
        return len(arguments)
    return 0


def _audit_tool_denial(
    store: HivemindStore,
    *,
    tool_name: str,
    operator_id: str,
    reason: str,
    arguments: Any,
) -> None:
    store.audit(
        "queen_bee.tool.denied",
        _queen_bee_actor(store, operator_id),
        QUEEN_BEE_TARGET_ID,
        "denied",
        reason,
        {
            "tool_name": tool_name,
            "operator_id": operator_id,
            "payload_key_count": _payload_key_count(arguments),
        },
    )


def _validate_arguments(tool: OperatorTool, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise StoreValidationError("arguments must be an object")
    normalized = dict(arguments)
    error = payload_schema_error(tool.input_schema, normalized)
    if error:
        raise StoreValidationError(error)
    return normalized


def execute_queen_bee_tool(
    store: HivemindStore,
    *,
    tool_name: str,
    arguments: Any,
    operator_id: str,
) -> dict[str, Any]:
    tool = OPERATOR_TOOLS.get(tool_name)
    if tool is None:
        raise StoreNotFoundError(f"unknown Queen Bee tool: {tool_name}")
    try:
        normalized_arguments = _validate_arguments(tool, arguments)
        if tool.mutates:
            store.get_agent(QUEEN_BEE_AGENT_ID)
        result = tool.handler(store, normalized_arguments, operator_id)
    except StoreError as exc:
        _audit_tool_denial(
            store,
            tool_name=tool.name,
            operator_id=operator_id,
            reason=str(exc),
            arguments=arguments,
        )
        raise

    if tool.mutates:
        store.audit(
            "queen_bee.tool.executed",
            QUEEN_BEE_AGENT_ID,
            QUEEN_BEE_TARGET_ID,
            "allowed",
            "Queen Bee operator tool executed",
            {
                "tool_name": tool.name,
                "operator_id": operator_id,
                "payload_key_count": _payload_key_count(arguments),
            },
        )
    return {"tool": tool.name, "actor_id": QUEEN_BEE_AGENT_ID if tool.mutates else operator_id, "result": result}
