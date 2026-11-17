"""Golden telemetry and redaction harness.

Plants canary secrets and personal data into a run, captures every place the Runtime writes
(run events, audit records, structured logs, OpenTelemetry spans, span events and metrics, usage
meter calls, and every persisted store row), asserts that no canary reaches any of them, and pins
the shape of the events, spans, audits, metrics and logs as golden files under
``tests/telemetry/golden``.

Regenerate the golden files after an intended change in telemetry shape with::

    ALGEN_UPDATE_GOLDEN=1 python -m pytest -q -p no:cacheprovider tests/telemetry

and review the diff: every changed line is a change hosts and dashboards will see.

Every canary is built at run time, so secret scanners never see a key-shaped literal, and none
of them is a real credential.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

GOLDEN_DIR = Path(__file__).parent / "golden"
UPDATE = os.environ.get("ALGEN_UPDATE_GOLDEN") == "1"

TENANT = "tenant-golden"
USER = "user-golden"


def _join(*parts: str) -> str:
    return "".join(parts)


# ---------------------------------------------------------------------------------------------
# Canaries. "Content" canaries arrive as data (user input, model output, tool arguments and
# results, retrieved documents, error messages): the Runtime must mask them by their shape or by
# the key they sit under. "Held" canaries are credentials the Runtime handles itself (a caller
# credential, an env:// secret, an MCP OAuth token, a provider API key): no sink may carry them
# at all, so they have no recognisable shape and only the Runtime never writing them keeps them
# out.

OPENAI_KEY = _join("sk-", "goldenCanary", "0123456789abcdefXYZ")
AWS_KEY = _join("AKIA", "GOLDENCANARY0001")
GITHUB_TOKEN = _join("ghp_", "GoldenCanary", "0123456789abcdefghijklmn")
JWT = _join("eyJ", "hbGciOiJIUzI1NiJ9", ".", "eyJ", "zdWIiOiJnb2xkZW4ifQ", ".", "Z29sZGVuU2ln")
DSN_PASSWORD = _join("gcDsnPw", "7q9Xz2")
BEARER = _join("gcBearer", ".7f3a9c-Tok_42")
CREDENTIAL_ARGUMENT = _join("gcCredArg", "55k3yZZ")
EMAIL = _join("golden.canary", "@", "example.com")
PHONE = _join("98765", "01234")
CARD = _join("4242 4242 ", "4242 4242")

CALLER_CREDENTIAL = _join("gc", "caller", "QmFzZTY0")
ENV_SECRET = _join("gc", "envsecret", "Wm9vbGFu")
MCP_OAUTH_TOKEN = _join("gc", "mcpoauth", "UGFzc3dk")
PROVIDER_API_KEY = _join("gc", "provkey", "TmV2ZXI")

SECRET_CANARIES: dict[str, str] = {
    "openai_key": OPENAI_KEY,
    "aws_key": AWS_KEY,
    "github_token": GITHUB_TOKEN,
    "jwt": JWT,
    "dsn_password": DSN_PASSWORD,
    "bearer": BEARER,
    "credential_argument": CREDENTIAL_ARGUMENT,
}
PII_CANARIES: dict[str, str] = {
    "email": EMAIL,
    "phone": PHONE,
    "card": CARD,
    # The card number as a sink may write it once spaces are dropped.
    "card_digits": CARD.replace(" ", ""),
}
HELD_CANARIES: dict[str, str] = {
    "caller_credential": CALLER_CREDENTIAL,
    "env_secret": ENV_SECRET,
    "mcp_oauth_token": MCP_OAUTH_TOKEN,
    "provider_api_key": PROVIDER_API_KEY,
}
ALL_CANARIES: dict[str, str] = {**SECRET_CANARIES, **PII_CANARIES, **HELD_CANARIES}


def leaky_text(label: str = "") -> str:
    """One string carrying every content canary, as a tool, a model or a user might write it."""
    prefix = f"[{label}] " if label else ""
    return (
        f"{prefix}openai {OPENAI_KEY} aws {AWS_KEY} github {GITHUB_TOKEN} jwt {JWT} "
        f"dsn postgresql://svc:{DSN_PASSWORD}@db.internal:5432/app "
        f"Authorization: Bearer {BEARER} password={CREDENTIAL_ARGUMENT} "
        f"mail {EMAIL} phone {PHONE} card {CARD}"
    )


def leaky_mapping(label: str = "") -> dict[str, Any]:
    """A structured payload carrying the canaries in text and under credential-named keys."""
    return {
        "note": leaky_text(label),
        "api_key": OPENAI_KEY,
        "password": CREDENTIAL_ARGUMENT,
        "nested": {"client_secret": GITHUB_TOKEN, "contact": EMAIL},
    }


# A value under a credential-named key in a tool's structured result (``{"password": ...}``) is
# the run's working data: the secrets guardrail masks credentials by their shape in text and
# leaves structured fields to the tool (``next_page_token`` must reach the model). The model,
# the run's messages and the tool-execution ledger (which replays the result) therefore keep it;
# every telemetry sink (events, spans, logs, metrics, audits, narrative) still masks it, by key.
KEYED_RESULT_CANARIES = frozenset({"credential_argument"})
WORKING_DATA = {
    "store:run_store": KEYED_RESULT_CANARIES,
    "store:tool_execution_ledger": KEYED_RESULT_CANARIES,
}


def find_canaries(haystack: str, canaries: Mapping[str, str] = ALL_CANARIES) -> list[str]:
    return sorted(name for name, value in canaries.items() if value in haystack)


def assert_no_canary(
    sinks: Mapping[str, str],
    canaries: Mapping[str, str] = ALL_CANARIES,
    *,
    scenario: str = "",
    allowed: Mapping[str, Iterable[str]] | None = None,
) -> None:
    """Fails naming every (sink, canary) pair that leaked. ``allowed`` names, per sink, the
    canaries that sink legitimately holds (see WORKING_DATA)."""
    debug = os.environ.get("ALGEN_GOLDEN_DEBUG_DIR")
    if debug:
        # Writes every sink to <dir>/<scenario>.json, to find where a canary sits.
        target = Path(debug) / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', scenario)}.json"
        target.write_text(json.dumps(dict(sinks), indent=1))
    permitted = {sink: set(names) for sink, names in (allowed or {}).items()}
    leaks = {
        sink: found
        for sink, text in sinks.items()
        if (
            found := [
                name
                for name in find_canaries(text, canaries)
                if name not in permitted.get(sink, ())
            ]
        )
    }
    context = []
    for sink, found in leaks.items():
        for name in found:
            at = sinks[sink].find(canaries[name])
            context.append(f"{sink}/{name}: ...{sinks[sink][max(0, at - 120) : at + 60]}...")
    assert not leaks, f"canaries leaked in {scenario or 'the scenario'}: {leaks}\n" + "\n".join(
        context
    )


# ---------------------------------------------------------------------------------------------
# Rendering anything (stores, models, dataclasses) as searchable text.


def deep_text(value: Any, *, _seen: set[int] | None = None, _depth: int = 0) -> str:
    """Every string reachable from ``value``: dict keys and values, sequences, Pydantic models,
    dataclasses and plain objects' attributes, for searching a store's whole contents."""
    seen = _seen if _seen is not None else set()
    if _depth > 40:
        return ""
    if isinstance(value, (str, bytes, bytearray)):
        return value if isinstance(value, str) else bytes(value).decode("utf-8", "replace")
    if isinstance(value, (int, float, bool)) or value is None or isinstance(value, Enum):
        return str(value)
    if id(value) in seen:
        return ""
    if type(value).__module__.startswith(("opentelemetry", "structlog", "asyncio", "logging")):
        # Tracers, meters and loggers a store holds are sinks of their own, read separately.
        return ""
    seen.add(id(value))
    parts: list[str] = []
    if isinstance(value, BaseModel):
        try:
            parts.append(value.model_dump_json())
        except Exception:
            parts.append(repr(value))
        return "\n".join(parts)
    if isinstance(value, Mapping):
        for key, item in list(value.items()):
            parts.append(deep_text(key, _seen=seen, _depth=_depth + 1))
            parts.append(deep_text(item, _seen=seen, _depth=_depth + 1))
        return "\n".join(parts)
    if isinstance(value, (list, tuple, set, frozenset)) or (
        isinstance(value, Sequence) and not isinstance(value, str)
    ):
        for item in list(value):
            parts.append(deep_text(item, _seen=seen, _depth=_depth + 1))
        return "\n".join(parts)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            parts.append(deep_text(getattr(value, item.name, None), _seen=seen, _depth=_depth + 1))
        return "\n".join(parts)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        for item in list(attributes.values()):
            if callable(item) and not isinstance(item, (BaseModel, Mapping)):
                continue
            parts.append(deep_text(item, _seen=seen, _depth=_depth + 1))
        return "\n".join(parts)
    return repr(value)


# ---------------------------------------------------------------------------------------------
# Capturing spans, metrics and logs.


@dataclasses.dataclass
class Telemetry:
    spans: InMemorySpanExporter
    reader: InMemoryMetricReader
    provider: TracerProvider
    raw_logs: list[dict[str, Any]]
    caplog: pytest.LogCaptureFixture
    capsys: pytest.CaptureFixture[str]
    _stdout: list[str] = dataclasses.field(default_factory=list)

    def finished_spans(self) -> list[ReadableSpan]:
        self.provider.force_flush()
        return sorted(self.spans.get_finished_spans(), key=lambda span: span.start_time or 0)

    def span_text(self) -> str:
        lines = []
        for span in self.finished_spans():
            lines.append(span.name)
            lines.append(json.dumps(dict(span.attributes or {}), default=str))
            lines.append(str(span.status.description or ""))
            for event in span.events:
                lines.append(event.name)
                lines.append(json.dumps(dict(event.attributes or {}), default=str))
            for link in span.links:
                lines.append(json.dumps(dict(link.attributes or {}), default=str))
            lines.append(json.dumps(dict(span.resource.attributes), default=str))
        return "\n".join(lines)

    def metric_points(self) -> list[tuple[str, dict[str, Any]]]:
        data = self.reader.get_metrics_data()
        points: list[tuple[str, dict[str, Any]]] = []
        if data is None:
            return points
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        points.append((metric.name, dict(point.attributes or {})))
        return points

    def metric_text(self) -> str:
        return json.dumps(self.metric_points(), default=str)

    def log_text(self) -> str:
        """Every log line: the JSON the service's logging writes (stdout), stdlib records at
        DEBUG and above, and their exception text."""
        out = self.capsys.readouterr()
        self._stdout.append(out.out + out.err)
        records = [
            f"{record.getMessage()} {record.exc_text or ''} {record.args!r}"
            for record in self.caplog.records
        ]
        return "\n".join([*self._stdout, *records])

    def raw_log_text(self) -> str:
        """What the Runtime asked structlog to log, before the service's redaction processor.

        Only held credentials are checked here: an exception's traceback reaches the processor
        as it was raised, and the processor masks it (``logs``)."""
        return json.dumps(self.raw_logs, default=str)

    def log_events(self) -> list[str]:
        return sorted({f"{entry.get('log_level')}:{entry.get('event')}" for entry in self.raw_logs})


class _RawCapture:
    """A structlog processor that keeps each event dict as the Runtime gave it."""

    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self.sink = sink

    def __call__(self, logger: Any, method: str, event: Any) -> Any:
        record = dict(event)
        record.setdefault("log_level", method)
        self.sink.append(record)
        return event


@contextmanager
def capture_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Telemetry]:
    """Routes every Runtime tracer and meter to in-memory exporters, and logs as the service
    configures them (JSON with the redaction processor) at DEBUG, keeping the raw event dicts
    too."""
    from algen_agent_runtime.observability import setup

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        trace, "get_tracer", lambda name, *args, **kwargs: provider.get_tracer(name)
    )
    monkeypatch.setattr(metrics, "get_meter", lambda name, *args, **kwargs: meters.get_meter(name))
    # The Runtime's catalogue instruments come from the meter provider (cached per provider).
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: meters)
    previous = structlog.get_config()
    raw: list[dict[str, Any]] = []
    setup.configure_logging(logging.DEBUG)
    configured = structlog.get_config()
    processors = list(configured["processors"])
    processors.insert(len(processors) - 2, _RawCapture(raw))
    structlog.configure(processors=processors)
    caplog.set_level(logging.DEBUG)
    try:
        yield Telemetry(exporter, reader, provider, raw, caplog, capsys)
    finally:
        structlog.configure(**previous)
        provider.shutdown()
        meters.shutdown()


# ---------------------------------------------------------------------------------------------
# Golden shapes.

# Attribute and payload values kept in the golden files: enumerations whose change is a change in
# meaning. Every other value is pinned only by its type.
_KEPT_VALUES = re.compile(
    r"^(span\.type|.*\.status|.*\.outcome|status|outcome|type|event_type|action|decision|"
    r"tool\.name|agent\.tool\.name|gen_ai\.tool\.name|tool|guardrail\.name|guardrail\.category|"
    r"guardrail\.triggered|policy\.boundary|reason_code|.*\.reason_code|error\.type|"
    r"tool\.side_effect|side_effect|role|kind|finish_reason|llm\.finish_reason|severity|"
    r"verification\.passed|tool\.result\.redacted|result_redacted|redacted)$"
)


def _shape(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _shape(v, str(k)) for k, v in sorted(value.items(), key=lambda i: str(i[0]))
        }
    if isinstance(value, (list, tuple)):
        shapes = [_shape(item, key) for item in value]
        unique: list[Any] = []
        for item in shapes:
            if item not in unique:
                unique.append(item)
        return unique
    if isinstance(value, Enum):
        value = value.value
    if key and _KEPT_VALUES.match(key) and isinstance(value, (str, bool)) and len(str(value)) < 80:
        return value
    if value is None:
        return "null"
    return type(value).__name__


def span_shapes(spans: Iterable[ReadableSpan]) -> dict[str, Any]:
    """Each span's name, parent, status, attribute shapes and events, in start order.

    The attributes most spans share (the agent's and the run's identity) are pinned once, as
    ``common_attributes``; each span then lists the attributes it adds and the common ones it
    lacks (``without_common``), so the golden files stay readable and every attribute is still
    pinned on every span."""
    spans = list(spans)
    names = {span.context.span_id: span.name for span in spans if span.context}
    attributes = [_shape(dict(span.attributes or {})) for span in spans]
    # The agent spans (agent.run, agent.step, agent.model.call, ...) carry the identity.
    agent_shapes = [
        shape for span, shape in zip(spans, attributes, strict=True) if _is_agent_span(span)
    ]
    counts: dict[str, int] = {}
    for shape in agent_shapes:
        for key, value in shape.items():
            entry = json.dumps([key, value], sort_keys=True)
            counts[entry] = counts.get(entry, 0) + 1
    common = dict(
        json.loads(entry) for entry, count in counts.items() if count * 2 >= len(agent_shapes) > 1
    )
    shapes = []
    for span, shape in zip(spans, attributes, strict=True):
        parent = names.get(span.parent.span_id) if span.parent else None
        shared = common if _is_agent_span(span) else {}
        item: dict[str, Any] = {
            "name": span.name,
            "parent": parent,
            "status": span.status.status_code.name,
            "attributes": {
                key: value
                for key, value in shape.items()
                if key not in shared or shared[key] != value
            },
        }
        missing = sorted(key for key in shared if key not in shape)
        if missing:
            item["without_common"] = missing
        if span.events:
            item["events"] = [
                {"name": event.name, "attributes": sorted((event.attributes or {}).keys())}
                for event in span.events
            ]
        shapes.append(item)
    return {"common_agent_attributes": common, "spans": _collapse_repeats(shapes)}


def _collapse_repeats(items: list[Any], max_period: int = 12) -> list[Any]:
    """Pin a block of spans that repeats back to back once: the streamed-text guard evaluates
    the output policies once per window, and how many windows a stream has depends on how its
    text was chunked and coalesced, not on what the Runtime records for each."""
    out: list[Any] = []
    for item in items:
        out.append(item)
        for period in range(1, min(max_period, len(out) // 2) + 1):
            if out[-period:] == out[-2 * period : -period]:
                del out[-period:]
                break
    return out


def _is_agent_span(span: ReadableSpan) -> bool:
    return span.name.startswith(("agent.", "conversation."))


def event_shapes(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Each event's type and payload shape, in order. A run of identical shapes (streamed
    deltas, whose count depends on chunking and coalescing) is pinned once."""
    shapes: list[dict[str, Any]] = []
    for event in events:
        data = event.model_dump(mode="json") if isinstance(event, BaseModel) else dict(event)
        payload = data.get("payload", data.get("data", {}))
        shape = {"type": data.get("type"), "payload": _shape(payload)}
        if not shapes or shapes[-1] != shape:
            shapes.append(shape)
    return shapes


def audit_shapes(audits: Iterable[Any]) -> list[dict[str, Any]]:
    shapes = []
    for audit in audits:
        data = audit.model_dump(mode="json")
        shapes.append(
            {
                "action": data.get("action"),
                "outcome": data.get("outcome"),
                "keys": sorted(data),
                "metadata": _shape(data.get("metadata") or {}),
            }
        )
    return sorted(shapes, key=lambda item: json.dumps(item, sort_keys=True))


def metric_shapes(points: Iterable[tuple[str, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """The Runtime's own instruments. A library's (FastAPI 0.163+ records
    ``http.server.request.duration`` once a meter provider is installed) depends on its
    version, so it is searched for canaries (``metric_text``) but not pinned."""
    seen: list[dict[str, Any]] = []
    for name, attributes in points:
        if not name.startswith("algen_agent_runtime."):
            continue
        item = {"name": name, "attributes": _shape(dict(attributes))}
        if item not in seen:
            seen.append(item)
    return sorted(seen, key=lambda item: json.dumps(item, sort_keys=True))


def assert_golden(name: str, shape: Mapping[str, Any]) -> None:
    """Compares ``shape`` with ``golden/<name>.json``; writes it when ALGEN_UPDATE_GOLDEN=1."""
    path = GOLDEN_DIR / f"{name}.json"
    rendered = json.dumps(shape, indent=2, sort_keys=True, default=str) + "\n"
    if UPDATE:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        return
    if not path.exists():
        pytest.fail(f"golden file {path.name} is missing: {REGENERATE}")
    expected = path.read_text()
    if json.loads(rendered) != json.loads(expected):
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"golden/{path.name}",
                tofile="this run",
                n=2,
            )
        )
        pytest.fail(
            f"the telemetry shape of {name!r} changed. If the change is intended, {REGENERATE} "
            f"and review the diff.\n{diff[:6000]}"
        )


REGENERATE = (
    "regenerate the golden files with "
    "ALGEN_UPDATE_GOLDEN=1 python -m pytest -q -p no:cacheprovider tests/telemetry"
)


def assert_same_shape_as_golden(name: str, shape: Mapping[str, Any], keys: Iterable[str]) -> None:
    """Compares ``keys`` of ``shape`` with ``golden/<name>.json`` without ever writing it: for a
    variant (another store backend) that must record what the golden scenario records. Logs are
    left out by the caller where the environment adds its own (a shared database's warnings)."""
    path = GOLDEN_DIR / f"{name}.json"
    if not path.exists():
        pytest.fail(f"golden file {path.name} is missing: {REGENERATE}")
    expected = json.loads(path.read_text())
    rendered = json.loads(json.dumps(shape, sort_keys=True, default=str))
    for key in keys:
        if rendered[key] != expected[key]:
            diff = "".join(
                difflib.unified_diff(
                    json.dumps(expected[key], indent=2, sort_keys=True).splitlines(keepends=True),
                    json.dumps(rendered[key], indent=2, sort_keys=True).splitlines(keepends=True),
                    fromfile=f"golden/{path.name}",
                    tofile="this run",
                    n=2,
                )
            )
            pytest.fail(f"{key} differ from golden/{path.name}\n{diff[:6000]}")
