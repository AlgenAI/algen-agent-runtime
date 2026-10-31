from __future__ import annotations

import sys
import types
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, Literal

import pytest
from pydantic import BaseModel, ValidationError

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError, ToolNotAttemptedError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.orchestration.describe import describe_components
from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime.policies.contracts import PolicyAction
from algen_agent_runtime.policies.engine import CompositePolicyEngine, SideEffectPolicy
from algen_agent_runtime.tools.authoring import (
    ApprovalRequest,
    Toolkit,
    destructive,
    external,
    long_running,
    read,
    tool,
    write,
)
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.tools.testing import (
    assert_tool_contract,
    fake_tool_context,
    tool_contract_problems,
)
from algen_agent_runtime.types.contracts import ApprovalPolicy


class Address(BaseModel):
    city: str
    postcode: str | None = None


class Account(BaseModel):
    id: str
    domain: str
    address: Address


class OwnerChange(BaseModel):
    account_id: str
    owner: str


class Receipt(BaseModel):
    account_id: str
    owner: str
    key: str


@tool(name="crm.search")
def search(query: str, limit: int = 10, region: Literal["eu", "us"] = "eu") -> list[str]:
    """Search accounts by name."""
    return [f"{query}-{region}"] * min(limit, 2)


@tool
async def lookup(domain: str, context: ToolContext) -> Account:
    """Find one account by its web domain."""
    return Account(id=context.tenant_id, domain=domain, address=Address(city="Pune"))


@tool(write(approval=True), name="crm.update_owner")
async def update_owner(change: OwnerChange, context: ToolContext) -> Receipt:
    """Change an account's owner."""
    return Receipt(account_id=change.account_id, owner=change.owner, key=context.idempotency_key)


async def _run(item: Tool, arguments: dict[str, Any]) -> Any:
    registry = ToolRegistry()
    registry.register(item)
    result = await ToolExecutor(registry, CompositePolicyEngine()).execute(
        item.definition.name, arguments, fake_tool_context(item)
    )
    return result.value


def test_schemas_come_from_type_hints() -> None:
    definition = search.definition

    assert definition.description == "Search accounts by name."
    assert definition.version == "1.0.0"
    assert definition.input_schema == {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
            "region": {"type": "string", "enum": ["eu", "us"], "default": "eu"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    assert definition.output_schema == {"type": "array", "items": {"type": "string"}}


def test_models_are_inlined_and_closed() -> None:
    output = lookup.definition.output_schema

    assert "$defs" not in output and "$ref" not in str(output)
    assert output["additionalProperties"] is False
    assert output["properties"]["address"]["additionalProperties"] is False
    assert "title" not in output["properties"]["address"]
    assert lookup.definition.input_schema["properties"] == {"domain": {"type": "string"}}


def test_a_single_model_parameter_is_the_whole_input() -> None:
    assert update_owner.definition.input_schema["properties"].keys() == {"account_id", "owner"}


async def test_sync_and_async_handlers_run_through_the_executor() -> None:
    assert await _run(search, {"query": "acme", "region": "us"}) == ["acme-us", "acme-us"]
    assert await _run(lookup, {"domain": "acme.com"}) == {
        "id": "test-tenant",
        "domain": "acme.com",
        "address": {"city": "Pune", "postcode": None},
    }


@tool(name="crm.move")
async def move(account_id: str, address: Address) -> str:
    """Move an account to a new address."""
    return f"{account_id}:{address.city}"


@pytest.mark.parametrize(
    ("item", "arguments"),
    [
        (update_owner, {"account_id": "a1", "owner": "kim", "admin": True}),
        (move, {"account_id": "a1", "address": {"city": "Pune", "admin": True}}),
    ],
)
async def test_a_direct_call_refuses_fields_the_schema_does_not_declare(
    item: Tool, arguments: dict[str, Any]
) -> None:
    # Address and OwnerChange ignore unknown fields, but the advertised schema closes them.
    with pytest.raises(ValidationError, match=r"admin\n  Extra inputs are not permitted"):
        await item.execute(arguments, fake_tool_context(item))

    assert (
        await move.execute(
            {"account_id": "a1", "address": {"city": "Pune"}}, fake_tool_context(move)
        )
        == "a1:Pune"
    )


def test_presets_reproduce_the_governed_tool_rules() -> None:
    def definition(preset: Any) -> ToolDefinition:
        def operate(record_id: str) -> dict[str, str]:
            """Operate on one record."""
            return {}

        return tool(preset, name="hr.operate")(operate).definition

    reading = definition(read())
    assert (reading.side_effect, reading.required_permissions, reading.idempotency) == (
        SideEffect.READ,
        frozenset({"hr.read"}),
        Idempotency.IDEMPOTENT,
    )
    assert (reading.timeout_seconds, reading.retry_policy.max_attempts) == (30, 3)
    for preset, effect in (
        (write(), SideEffect.WRITE),
        (external(), SideEffect.EXTERNAL),
        (destructive(), SideEffect.DESTRUCTIVE),
    ):
        changing = definition(preset)
        assert changing.side_effect == effect
        assert changing.required_permissions == frozenset({"hr.write"})
        assert changing.idempotency == Idempotency.KEYED
        assert changing.retry_policy.max_attempts == 1
    assert definition(destructive()).requires_approval is True
    assert definition(write()).requires_approval is False
    slow = definition(long_running(write()))
    assert (slow.timeout_seconds, slow.retry_policy.max_attempts, slow.side_effect) == (
        180,
        1,
        SideEffect.WRITE,
    )
    slow_read = definition(long_running())
    assert (slow_read.side_effect, slow_read.retry_policy.max_attempts) == (SideEffect.READ, 1)
    custom = definition(write().replace(timeout_seconds=5, permissions=frozenset({"hr.admin"})))
    assert (custom.timeout_seconds, custom.required_permissions) == (5, frozenset({"hr.admin"}))


async def test_approval_true_requires_runtime_approval_whatever_the_agent_policy() -> None:
    lenient = ApprovalPolicy(require_for_side_effects=False, require_for_destructive=False)

    decision = await SideEffectPolicy().evaluate(
        "plan_tool", None, {"tool": update_owner.definition, "approval_policy": lenient}
    )

    assert update_owner.definition.requires_approval is True
    assert decision.action == PolicyAction.REQUIRE_APPROVAL


class ChangeRequests:
    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def handle(self, request: ApprovalRequest, execute: Callable[[], Awaitable[Any]]) -> Any:
        self.requests.append(request)
        if request.arguments.get("owner") == "approved":
            return await execute()
        return {"status": "APPROVAL_REQUESTED"}


async def test_an_approval_handler_wraps_the_call() -> None:
    flow = ChangeRequests()

    @tool(write(approval=flow), name="crm.reassign")
    async def reassign(account_id: str, owner: str) -> dict[str, str]:
        """Reassign an account."""
        return {"owner": owner}

    assert reassign.definition.requires_approval is False
    assert reassign.definition.audit_metadata == {"approval": "deployment"}
    assert await _run(reassign, {"account_id": "a", "owner": "bo"}) == {
        "status": "APPROVAL_REQUESTED"
    }
    assert await _run(reassign, {"account_id": "a", "owner": "approved"}) == {"owner": "approved"}
    assert [request.definition.name for request in flow.requests] == ["crm.reassign"] * 2


def test_authoring_errors_are_configuration_errors() -> None:
    def undocumented(value: str) -> str:
        return value

    def untyped(value) -> str:  # type: ignore[no-untyped-def]
        """Has no hint."""
        return str(value)

    with pytest.raises(ConfigurationError, match="needs a docstring"):
        tool(undocumented)
    with pytest.raises(ConfigurationError, match="needs a type hint"):
        tool(untyped)


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.closed = False
        FakeClient.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


async def _open_client(secrets: Callable[[str], Awaitable[str]]) -> FakeClient:
    return FakeClient(await secrets("env://CRM_TOKEN"))


crm = Toolkit("crm", env=("CRM_TOKEN",), client=_open_client)


@crm.tool(read())
async def find_account(domain: str) -> dict[str, str]:
    """Find one account by its web domain."""
    client = await crm.client()
    return {"domain": domain, "token": client.token}


@crm.tool(write(approval=True))
async def set_owner(account_id: str, owner: str) -> dict[str, str]:
    """Change an account's owner."""
    return {"account_id": account_id, "owner": owner}


@plugin(PluginManifest(name="acme.crm", version="1.0.0", publisher="acme"))
class CrmPlugin:
    def register(self, context: Any) -> None:
        crm.register(context)


@pytest.fixture
def crm_settings(monkeypatch: pytest.MonkeyPatch) -> AppSettings:
    module = types.ModuleType("acme.crm")
    module.CrmPlugin = CrmPlugin  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "acme", types.ModuleType("acme"))
    monkeypatch.setitem(sys.modules, "acme.crm", module)
    FakeClient.instances.clear()
    return AppSettings.model_validate(
        {
            "providers": {"local": {"type": "mock", "default_model": "m"}},
            "agents": [
                {
                    "name": "operator",
                    "version": "1.0.0",
                    "description": "d",
                    "system_instructions": "x",
                    "default_model": {"name": "m", "provider": "local"},
                    "enabled_tools": ["crm.find_account", "crm.set_owner"],
                    "domain_plugins": ["acme.crm:CrmPlugin"],
                }
            ],
            "security": {"trusted_plugin_prefixes": ["acme"]},
            "runtime": {"recover_incomplete_runs": False},
        }
    )


def test_toolkit_names_and_permissions() -> None:
    assert [item.definition.name for item in crm.tools] == ["crm.find_account", "crm.set_owner"]
    assert find_account.definition.required_permissions == frozenset({"crm.read"})
    assert set_owner.definition.required_permissions == frozenset({"crm.write"})


async def test_a_toolkit_registers_through_a_plugin_and_shares_one_client(
    crm_settings: AppSettings,
) -> None:
    inventory = describe_components(crm_settings)
    assert FakeClient.instances == []
    assert inventory.plugins[0].env == ("CRM_TOKEN",)
    assert "CRM_TOKEN" in inventory.environment
    assert {t.name: t.requires_approval for t in inventory.tools if t.source != "runtime"} == {
        "crm.find_account": False,
        "crm.set_owner": True,
    }

    container = build_container(crm_settings, environment={"CRM_TOKEN": "secret-token"})
    await container.astart()
    executor = ToolExecutor(container.tools, CompositePolicyEngine())
    for _ in range(2):
        result = await executor.execute(
            "crm.find_account", {"domain": "acme.com"}, fake_tool_context(find_account)
        )
        assert result.value == {"domain": "acme.com", "token": "secret-token"}
    await container.aclose()

    [client] = FakeClient.instances
    assert client.closed is True


async def test_a_toolkit_that_cannot_open_its_client_degrades_the_service(
    crm_settings: AppSettings,
) -> None:
    """A missing credential at start: the service comes up degraded, and the toolkit's calls
    are refused as not attempted rather than crashing the boot (#83 item 10)."""
    container = build_container(crm_settings, environment={})
    await container.astart()
    try:
        assert "toolkit.crm" in container.degraded_dependencies
        _ready, body = await container.readiness_report()
        assert "toolkit.crm" in str(body)
        executor = ToolExecutor(container.tools, CompositePolicyEngine())
        with pytest.raises(ToolNotAttemptedError, match="toolkit 'crm' is not available"):
            await executor.execute(
                "crm.find_account", {"domain": "acme.com"}, fake_tool_context(find_account)
            )
    finally:
        await container.aclose()


async def test_a_required_toolkit_that_cannot_start_stops_the_service(
    crm_settings: AppSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm, "required", True)
    container = build_container(crm_settings, environment={})
    with pytest.raises(ToolNotAttemptedError):
        await container.astart()
    await container.aclose()


async def test_a_toolkit_configuration_error_keeps_its_type_and_message() -> None:
    """The toolkit's own message says what to set or install, and callers catching
    ConfigurationError still catch it; the call is still recorded as not attempted."""

    def needs_extra(secrets: Any) -> Any:
        raise ConfigurationError("the crm connector needs 'crmlib'; pip install 'acme[crm]'")

    toolkit = Toolkit("crm2", client=needs_extra)
    with pytest.raises(ConfigurationError, match=r"pip install 'acme\[crm\]'") as caught:
        await toolkit.client()
    assert isinstance(caught.value, ToolNotAttemptedError)


async def test_any_other_client_error_keeps_only_its_type() -> None:
    def leaks(secrets: Any) -> Any:
        raise RuntimeError("connect failed for token=s3cr3t")

    toolkit = Toolkit("crm3", client=leaks)
    with pytest.raises(ToolNotAttemptedError) as caught:
        await toolkit.client()
    assert not isinstance(caught.value, ConfigurationError)
    assert "s3cr3t" not in str(caught.value) and "RuntimeError" in str(caught.value)


def test_authored_tools_meet_the_tool_contract() -> None:
    for item in (search, lookup, update_owner, find_account, set_owner):
        assert_tool_contract(item)


def test_the_contract_check_reports_every_problem() -> None:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        return None

    loose = Tool(
        ToolDefinition(
            name="crm.loose",
            version="1",
            description=" ",
            input_schema={
                "type": "object",
                "properties": {"nested": {"type": "object", "properties": {"a": {}}}},
            },
            output_schema={},
            side_effect=SideEffect.WRITE,
            idempotency=Idempotency.NON_IDEMPOTENT,
            timeout_seconds=10,
        ),
        handler,
    )

    problems = tool_contract_problems(loose, longest_wait_seconds=20)

    assert problems == (
        "the description is empty",
        "the input schema leaves / open; set additionalProperties: false",
        "the input schema leaves /nested open; set additionalProperties: false",
        "the output schema is empty",
        "a write tool requires no permission",
        "a write tool must be keyed by the idempotency key",
        "a write tool is retried 3 times; a retry after a timeout could repeat the change, "
        "so allow one attempt",
        "the timeout (10.0s) does not exceed the longest wait (20s)",
    )
    with pytest.raises(AssertionError, match="breaks the tool contract"):
        assert_tool_contract(loose)


def test_fake_context_grants_the_tool_permissions() -> None:
    first = fake_tool_context(set_owner)
    second = fake_tool_context(permissions={"x.read"}, tenant_id="t")

    assert first.permissions == frozenset({"crm.write"})
    assert (second.permissions, second.tenant_id) == (frozenset({"x.read"}), "t")
    assert first.idempotency_key != fake_tool_context(set_owner).idempotency_key


def test_a_dict_result_describes_like_a_hand_built_object_schema() -> None:
    @tool(write(), name="hr.update")
    async def update(record_id: str) -> dict[str, Any]:
        """Update one record."""
        return {}

    assert update.definition.output_schema == {"type": "object"}
