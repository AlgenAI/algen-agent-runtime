"""The HTTP API behind OIDC, in-process: scopes from claims, approvals, ownership, revocation.

The Runtime's FastAPI app is served through ``httpx.ASGITransport``; the identity provider (a
discovery document and a JWKS) and the upstream LMS are ``httpx.MockTransport`` stubs, and the
signing key is generated at start-up. Three users of tenant ``acme``: alice and bob (members) and
carol (an approver who may also revoke tokens).

Needs the ``auth`` extra: ``pip install 'algen-agent-runtime[auth]'``. Without it the example
says so and exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import secrets
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import httpx
import structlog
from fastapi import Depends, Request

import algen_agent_runtime.tools.http_api as http_api
from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.api.dependencies import Principal, principal_dependency, require_scope
from algen_agent_runtime.auth.oidc import map_claims
from algen_agent_runtime.config.settings import (
    AppSettings,
    EnvironmentSecretProvider,
    load_settings,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.security.network import check_host_allowlist, normalize_and_validate_url
from algen_agent_runtime.tools.http_api import http_api_tool
from algen_agent_runtime.types.contracts import RunRequest

if TYPE_CHECKING:
    from examples.pattern_secured_api.fakes import FakeLms, LocalIdentityProvider

CONFIG_PATH = Path(__file__).resolve().with_name("agent.yaml")
TENANT_ID = "acme"
SKIP_MESSAGE = (
    "SKIPPED: pattern_secured_api needs the auth extra (PyJWT and cryptography) to sign and "
    "verify tokens. Install it with: pip install 'algen-agent-runtime[auth]'"
)
SETTLED = {"completed", "failed", "cancelled", "awaiting_approval", "awaiting_reconciliation"}


def say(text: str = "") -> None:
    print(text, flush=True)


def auth_extra_installed() -> bool:
    try:
        import cryptography  # noqa: F401
        import jwt  # noqa: F401
    except ImportError:
        return False
    return True


def logs_to_stderr() -> None:
    """The Runtime logs JSON lines to stdout; send them to stderr so the walkthrough reads cleanly.

    Refused tokens (``authentication failed``) and the failed run in step 4 are logged there.
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))


@contextmanager
def offline_dns() -> Iterator[None]:
    """The LMS host is fake, so skip only the DNS step of the outbound check.

    The URL and host-allowlist checks still run; the request itself goes to ``FakeLms`` through
    its mock transport. A deployment never does this.
    """

    async def check(url: str, allowed_hosts: tuple[str, ...], _private: bool) -> list[str]:
        host, _ = normalize_and_validate_url(url)
        check_host_allowlist(host, allowed_hosts)
        return []

    with mock.patch.object(http_api, "validate_outbound_url", check):
        yield


def build(settings: AppSettings, idp: LocalIdentityProvider, lms: FakeLms) -> Container:
    """The container from agent.yaml, with two in-process substitutions for the network.

    The declared ``lms.my_courses`` entry is built with the fake LMS's transport as its client,
    and OIDC discovery and JWKS fetches go to the local identity provider.
    """
    lms_tool = http_api_tool(
        settings.http_tools[0],
        allowed_hosts=settings.security.allowed_http_hosts,
        secrets=EnvironmentSecretProvider().get,
        client=httpx.AsyncClient(transport=lms.transport),
    )
    container = build_container(
        settings.model_copy(update={"http_tools": ()}),
        additional_tools=(lms_tool,),
    )
    return dataclasses.replace(container, oidc_transport=idp.transport)


async def settle(container: Container, run_id: str) -> None:
    """Let a run started over HTTP reach a pause or an end."""
    for _ in range(500):
        state = await container.runtime.status(run_id, TENANT_ID)
        if state.status.value in SETTLED:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(run_id)


async def run() -> dict[str, Any]:
    from examples.pattern_secured_api.fakes import FakeLms, LocalIdentityProvider

    settings = load_settings((CONFIG_PATH,))
    idp = LocalIdentityProvider()
    lms = FakeLms(idp, enrolments={"alice": ["Statistics 101", "Data Ethics"]})
    container = build(settings, idp, lms)
    runtime = container.runtime
    app = create_app(settings, container)
    logs_to_stderr()

    # A host route: the HTTP API's run endpoints pass no caller credentials, so a host that needs
    # them authenticates the request the same way and calls the Runtime itself.
    authenticate = principal_dependency(
        settings.api,
        audits=runtime.audits,
        revocations=container.token_revocations,
        oidc_transport=idp.transport,
    )

    signed_in = Depends(authenticate)

    @app.post("/host/my-courses")
    async def my_courses(request: Request, principal: Principal = signed_in) -> dict[str, Any]:
        require_scope(principal, "runs:write")
        result = await runtime.run(
            RunRequest(
                agent="course-assistant",
                input="Which courses am I enrolled in?",
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
            ),
            credentials={"authorization": request.headers["authorization"]},
        )
        return {"run_id": result.run_id, "status": result.status.value, "output": result.output}

    alice = idp.issue("alice", roles=["member"])
    bob = idp.issue("bob", roles=["member"])
    carol = idp.issue("carol", roles=["approver"], scope="auth:revoke")
    summary: dict[str, Any] = {}
    ops_model = runtime.router.provider("ops-model")
    lms_model = runtime.router.provider("lms-model")
    assert isinstance(ops_model, MockModelProvider) and isinstance(lms_model, MockModelProvider)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runtime.invalid"
        ) as http,
    ):
        say("== 1. Tokens and scopes from claims ==")
        issuer = settings.api.oidc.issuers[0]
        for name, issued in (("alice", alice), ("bob", bob), ("carol", carol)):
            _, user, scopes = map_claims(issued.claims, issuer.claims)
            say(f"{name}: user={user} scopes={sorted(scopes)}")
        anonymous = await http.get("/v1/approvals")
        say(f"no token -> {anonymous.status_code}")
        forged = idp.forge("alice", roles=["approver"])
        refused = await http.get("/v1/approvals", headers=forged.header)
        say(f"token signed by another key -> {refused.status_code}")

        say()
        say("== 2. An approval pause and the approval inbox ==")
        proposed = secrets.token_urlsafe(18)
        ops_model.queue(
            tool_call_response(
                "webhooks.register",
                {"url": "https://hooks.acme.example/billing", "secret": proposed},
            ),
            "The billing webhook is registered.",
        )
        started = await http.post(
            "/v1/runs",
            headers=alice.header,
            json={"agent": "webhook-admin", "input": "Register the billing webhook"},
        )
        run_id = started.json()["run_id"]
        await settle(container, run_id)
        status = (await http.get(f"/v1/runs/{run_id}", headers=alice.header)).json()["status"]
        say(f"alice starts a run -> {status}")
        denied = await http.get("/v1/approvals", headers=alice.header)
        say(f"alice GET /v1/approvals -> {denied.status_code} ({denied.json()['detail']})")
        inbox = (
            await http.get("/v1/approvals", params={"status": "pending"}, headers=carol.header)
        ).json()
        [pending] = inbox
        say(
            f"carol GET /v1/approvals -> {len(inbox)} pending: {pending['proposed_action']} "
            f"{pending['redacted_parameters']}"
        )
        replacement = secrets.token_urlsafe(18)
        decided = await http.post(
            f"/v1/runs/{run_id}/approval",
            headers=carol.header,
            json={
                "decision": "modified",
                "modified_parameters": {
                    "url": "https://hooks.acme.example/billing",
                    "secret": replacement,
                },
            },
        )
        await settle(container, run_id)
        final = (await http.get(f"/v1/runs/{run_id}", headers=alice.header)).json()
        say(f"carol modifies and approves -> {decided.status_code}; run {final['status']}")
        [decision] = (
            await http.get("/v1/approvals", params={"run_id": run_id}, headers=carol.header)
        ).json()
        say(f"inbox shows {decision['status']} with {decision['modified_parameters']}")
        summary["approval"] = {"status": final["status"], "shown": decision["modified_parameters"]}

        say()
        say("== 3. Conversation ownership ==")
        conversation = (
            await http.post("/v1/conversations", headers=alice.header, json={"agent": "helpdesk"})
        ).json()
        conversation_id = conversation["id"]
        await http.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=alice.header,
            json={"text": "When is the next maintenance window?"},
        )
        turn_run = ""
        for _ in range(500):
            messages = (
                await http.get(
                    f"/v1/conversations/{conversation_id}/messages", headers=alice.header
                )
            ).json()
            replies = [
                m for m in messages if m["role"] == "assistant" and m["status"] == "completed"
            ]
            if replies:
                turn_run = replies[-1]["run_ids"][-1]
                break
            await asyncio.sleep(0.01)
        owner_view = await http.get(f"/v1/runs/{turn_run}", headers=alice.header)
        other_view = await http.get(f"/v1/runs/{turn_run}", headers=bob.header)
        other_conversation = await http.get(
            f"/v1/conversations/{conversation_id}", headers=bob.header
        )
        say(f"alice GET /v1/runs/<her turn's run> -> {owner_view.status_code}")
        say(f"bob GET /v1/runs/<alice's turn's run> -> {other_view.status_code}")
        say(f"bob GET /v1/conversations/<alice's> -> {other_conversation.status_code}")
        summary["ownership"] = (owner_view.status_code, other_view.status_code)

        say()
        say("== 4. Calling the LMS as the signed-in user (auth.type: caller) ==")
        with offline_dns():
            lms_model.queue(tool_call_response("lms.my_courses", {}), "You have two courses.")
            hosted = (await http.post("/host/my-courses", headers=alice.header)).json()
            say(
                f"host route runs course-assistant with alice's credential -> {hosted['status']}: "
                f"{hosted['output']}"
            )
            say(f"LMS saw: {lms.seen}")
            seen_before = len(lms.seen)
            lms_model.queue(tool_call_response("lms.my_courses", {}), "You have two courses.")
            started = await http.post(
                "/v1/runs",
                headers=alice.header,
                json={"agent": "course-assistant", "input": "Which courses am I enrolled in?"},
            )
            bare_run = started.json()["run_id"]
            await settle(container, bare_run)
            bare = (await http.get(f"/v1/runs/{bare_run}", headers=alice.header)).json()
            say(f"POST /v1/runs (no caller credential) -> {bare['status']}: {bare['error']}")
            say(f"requests the LMS received for it: {len(lms.seen) - seen_before}")
        summary["caller"] = {"hosted": hosted["status"], "bare": bare["status"]}

        say()
        say("== 5. Token revocation ==")
        before = await http.get("/v1/conversations", headers=bob.header)
        expires_at = datetime.fromtimestamp(bob.claims["exp"], UTC).isoformat()
        revoked = await http.post(
            "/v1/auth/revocations/tokens",
            headers=carol.header,
            json={"jti": bob.claims["jti"], "expires_at": expires_at, "reason": "laptop lost"},
        )
        after = await http.get("/v1/conversations", headers=bob.header)
        still = await http.get("/v1/conversations", headers=alice.header)
        say(f"bob before -> {before.status_code}")
        say(f"carol POST /v1/auth/revocations/tokens (bob's jti) -> {revoked.status_code}")
        say(f"bob after -> {after.status_code}; alice -> {still.status_code}")
        summary["revocation"] = (before.status_code, after.status_code)
        say(
            f"identity provider requests: {len(idp.fetches)} "
            "(discovery and JWKS once each for the API and the host route, then cached)"
        )
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the secured HTTP API pattern offline")
    parser.parse_args(argv)
    if not auth_extra_installed():
        say(SKIP_MESSAGE)
        return
    asyncio.run(run())


if __name__ == "__main__":
    main()
