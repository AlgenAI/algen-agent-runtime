"""High-level HTML generation and export helpers for Agent Manifest UI."""

from __future__ import annotations

import os
from pathlib import Path

from algen_agent_runtime.config.settings import AppSettings, load_settings
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.ui.spec import extract_agent_manifest_spec
from algen_agent_runtime.ui.template import render_agent_ui


def generate_agent_ui_html(
    settings: AppSettings | None = None,
    container: Container | None = None,
    raw_yaml: str | None = None,
    live_server: bool = False,
    api_base_url: str = "",
    title: str | None = None,
) -> str:
    """Generate self-contained Agent Manifest UI HTML document."""
    resolved_settings = settings or AppSettings()
    spec = extract_agent_manifest_spec(
        settings=resolved_settings,
        container=container,
        raw_yaml=raw_yaml,
        live_server=live_server,
        api_base_url=api_base_url,
        title=title,
    )
    return render_agent_ui(spec)


def export_agent_ui_html(
    config_files: tuple[str | Path, ...] | None = None,
    settings: AppSettings | None = None,
    output_path: str | Path = "agent-ui.html",
    title: str | None = None,
) -> Path:
    """Load configuration, generate standalone static HTML, and write to destination path."""
    raw_yaml: str | None = None
    if config_files:
        # If config files are specified, read raw content from the primary file
        primary_file = Path(config_files[0])
        if primary_file.exists():
            try:
                raw_yaml = primary_file.read_text(encoding="utf-8")
            except OSError:
                pass
        resolved_settings = load_settings(config_files)
    elif settings is not None:
        resolved_settings = settings
    else:
        # Check environment or local directory
        env_config = os.getenv("ALGEN_AGENT_RUNTIME_CONFIG", "")
        if env_config:
            paths = tuple(Path(p) for p in env_config.split(os.pathsep) if p)
            if paths and paths[0].exists():
                try:
                    raw_yaml = paths[0].read_text(encoding="utf-8")
                except OSError:
                    pass
            resolved_settings = load_settings(paths)
        else:
            # Check standard local filenames
            candidates = [
                Path("agent.yaml"),
                Path("config/agent.yaml"),
                Path("algen-agent-runtime.yaml"),
            ]
            found = next((c for c in candidates if c.exists()), None)
            if found:
                try:
                    raw_yaml = found.read_text(encoding="utf-8")
                except OSError:
                    pass
                resolved_settings = load_settings((found,))
            else:
                resolved_settings = AppSettings()

    html = generate_agent_ui_html(
        settings=resolved_settings,
        raw_yaml=raw_yaml,
        live_server=False,
        title=title,
    )

    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    return dest
