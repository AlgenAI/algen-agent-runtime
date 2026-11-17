"""What the pattern_observability and pattern_run_follower examples demonstrate, offline."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from algen_agent_runtime.observability.metrics import RUNTIME_METRICS, prometheus_name
from algen_agent_runtime_examples.pattern_run_follower.app import run as follow_runs

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "algen_agent_runtime_examples" / "pattern_observability" / "deploy"


def test_observability_example_records_the_run_level_metrics() -> None:
    # A subprocess: the example installs the process-global meter provider, which can be set once.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ALGEN_AGENT_RUNTIME__")
    }
    result = subprocess.run(
        [sys.executable, "-m", "algen_agent_runtime_examples.pattern_observability.app"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    printed = result.stdout

    assert result.returncode == 0, result.stderr[-4000:]
    for line in (
        "1. lookup           -> completed",
        "2. reorder approved  -> awaiting_approval, decision 202, completed",
        "3. reorder rejected  -> awaiting_approval, decision 202, failed",
        "4. injection        -> failed",
        "5. hand count       -> DELETE 200, cancelled while stock.audit ran",
        "6. without runs:write -> 403",
        # The series the dashboards and alert rules query, under their Prometheus names.
        '{agent="stock-assistant", run_kind="run"} 5',
        '{agent="stock-assistant", reason="approval_rejected", run_kind="run", status="failed"} 1',
        '{agent="stock-assistant", reason="none", run_kind="run", status="completed"} 2',
        '{approval_kind="tool", outcome="approved"} 1',
        '{approval_kind="tool", outcome="rejected"} 1',
        '{outcome="cancelled", tool="stock.audit"} 1',
        '{boundary="input", policy="prompt_injection"} 1',
        '{method="POST", route="/v1/runs", status_code="403"} 1',
    ):
        assert line in printed, line
    assert "algen_agent_runtime_runs_finished_total" in printed
    assert "algen_agent_runtime_run_duration_seconds_count" in printed


def test_observability_stack_mounts_the_shipped_dashboards_and_rules() -> None:
    compose = yaml.safe_load((DEPLOY / "compose.yaml").read_text())
    mounts = [
        volume.split(":")[0]
        for service in compose["services"].values()
        for volume in service.get("volumes", ())
    ]
    for source in mounts:
        assert (DEPLOY / source).resolve().exists(), source
    assert "../../../deploy/observability/grafana" in mounts
    assert "../../../deploy/observability/prometheus/alerts.yml" in mounts
    assert all(
        name.startswith("algen-rt-ex-")
        for name in (service["container_name"] for service in compose["services"].values())
    )
    collector = yaml.safe_load((DEPLOY / "otel-collector.yaml").read_text())
    assert collector["service"]["pipelines"]["metrics"] == {
        "receivers": ["otlp"],
        "exporters": ["prometheus"],
    }
    prometheus = yaml.safe_load((DEPLOY / "prometheus.yml").read_text())
    assert prometheus["rule_files"] == ["/etc/prometheus/rules/alerts.yml"]
    # The README's metric names are the catalogue's.
    readme = (DEPLOY.parent / "README.md").read_text()
    names = {prometheus_name(spec) for spec in RUNTIME_METRICS}
    assert "algen_agent_runtime_runs_finished_total" in names
    assert "algen_agent_runtime_runs_finished_total" in readme


async def test_run_follower_discards_retracted_text_and_cancels() -> None:
    outcome = await follow_runs()

    assert outcome["retracted"] == {
        "status": "completed",
        "retractions": ["verification"],
        "shown_is_output": True,
    }
    assert outcome["cancelled"] == {
        "status": "cancelled",
        "run_cancelled_events": 1,
        "last_event": "run.cancelled",
        "tool_cancelled": True,
    }
    assert outcome["deadline"] == {"timed_out": True, "status": "cancelled"}
