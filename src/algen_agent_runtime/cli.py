"""CLI entry point for Algen Agent Runtime."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from algen_agent_runtime.scaffold import AVAILABLE_TEMPLATES, scaffold_project


def _run_serve(
    host: str | None = None,
    port: int | None = None,
    config: list[str] | None = None,
    agent_ui: bool | None = None,
) -> None:
    import uvicorn

    from algen_agent_runtime.api.app import create_app
    from algen_agent_runtime.config.settings import AppSettings, load_settings

    config_files: list[str] = []
    if config:
        config_files.extend(config)
    else:
        env_config = os.getenv("ALGEN_AGENT_RUNTIME_CONFIG", "")
        if env_config:
            config_files.extend(filter(None, env_config.split(os.pathsep)))

    settings = (
        load_settings(tuple(Path(item) for item in config_files)) if config_files else AppSettings()
    )

    if agent_ui is not None:
        settings = settings.model_copy(
            update={"api": settings.api.model_copy(update={"agent_ui_enabled": agent_ui})}
        )

    bind_host = host or settings.api.host
    bind_port = port if port is not None else settings.api.port

    if settings.api.agent_ui_enabled:
        print(f"Agent UI enabled at: http://{bind_host}:{bind_port}{settings.api.agent_ui_path}")

    uvicorn.run(create_app(settings), host=bind_host, port=bind_port)


def _run_generate_ui(
    config: list[str] | None = None,
    output: str = "agent-ui.html",
    title: str | None = None,
    open_browser: bool = False,
) -> None:
    from algen_agent_runtime.config.settings import AppSettings, load_settings
    from algen_agent_runtime.ui.generator import generate_agent_ui_html

    config_paths: tuple[Path, ...] | None = None
    raw_yaml: str | None = None

    if config:
        config_paths = tuple(Path(item) for item in config)
    else:
        env_config = os.getenv("ALGEN_AGENT_RUNTIME_CONFIG", "")
        if env_config:
            config_paths = tuple(Path(item) for item in filter(None, env_config.split(os.pathsep)))
        else:
            candidates = [
                Path("agent.yaml"),
                Path("config/agent.yaml"),
                Path("algen-agent-runtime.yaml"),
            ]
            found = next((c for c in candidates if c.exists()), None)
            if found:
                config_paths = (found,)

    if config_paths and config_paths[0].exists():
        try:
            raw_yaml = config_paths[0].read_text(encoding="utf-8")
        except OSError:
            pass

    settings = load_settings(config_paths) if config_paths else AppSettings()

    html = generate_agent_ui_html(
        settings=settings,
        raw_yaml=raw_yaml,
        live_server=False,
        title=title,
    )

    if output == "-":
        sys.stdout.write(html)
        return

    dest = Path(output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    print(f"Generated static Agent UI at: {dest.resolve()}")

    if open_browser:
        webbrowser.open(dest.resolve().as_uri())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="algen-agent-runtime",
        description="Algen Agent Runtime command-line interface",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # serve command
    serve_parser = subparsers.add_parser("serve", help="Start the runtime HTTP/REST API server")
    serve_parser.add_argument("--host", default=None, help="Bind host address")
    serve_parser.add_argument("--port", type=int, default=None, help="Bind port number")
    serve_parser.add_argument(
        "--config",
        nargs="*",
        default=None,
        help="Path(s) to configuration YAML file(s)",
    )
    serve_parser.add_argument(
        "--ui",
        "--agent-ui",
        dest="agent_ui",
        action="store_true",
        default=None,
        help="Enable the built-in Agent YAML UI endpoint at /agent-ui",
    )

    # new command
    new_parser = subparsers.add_parser("new", help="Scaffold a new agent or workflow project")
    new_parser.add_argument("name", help="Name of the new agent or project")
    new_parser.add_argument(
        "--template",
        choices=AVAILABLE_TEMPLATES,
        default="agent",
        help="Template to scaffold: agent (default), approval, or workflow",
    )
    new_parser.add_argument(
        "--dir",
        default=None,
        help="Target directory (defaults to ./<name>)",
    )
    new_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite files in target directory if it already exists and is not empty",
    )

    # ui / export-ui command
    for cmd_name, cmd_help in [
        ("ui", "Generate standalone static HTML for the Agent YAML UI"),
        ("export-ui", "Alias for 'ui': generate standalone static HTML for the Agent YAML UI"),
    ]:
        ui_parser = subparsers.add_parser(cmd_name, help=cmd_help)
        ui_parser.add_argument(
            "--config",
            "-c",
            nargs="*",
            default=None,
            help="Path(s) to configuration YAML file(s) (defaults to agent.yaml or config/agent.yaml)",
        )
        ui_parser.add_argument(
            "--output",
            "-o",
            default="agent-ui.html",
            help="Target HTML output path (default: agent-ui.html, use '-' for stdout)",
        )
        ui_parser.add_argument(
            "--title",
            default=None,
            help="Custom title for the Agent UI",
        )
        ui_parser.add_argument(
            "--open",
            action="store_true",
            help="Open the generated HTML in the default web browser",
        )

    return parser


def main(argv: list[str] | None = None) -> None:
    args_list = sys.argv[1:] if argv is None else argv

    # Backward compatibility with 0.1.x: running without arguments starts the server
    if not args_list:
        _run_serve()
        return

    parser = build_parser()
    args = parser.parse_args(args_list)

    if args.command == "serve":
        _run_serve(host=args.host, port=args.port, config=args.config, agent_ui=args.agent_ui)
    elif args.command in {"ui", "export-ui"}:
        _run_generate_ui(
            config=args.config,
            output=args.output,
            title=args.title,
            open_browser=args.open,
        )
    elif args.command == "new":
        target = Path(args.dir) if args.dir else Path.cwd() / args.name
        try:
            dest = scaffold_project(
                name=args.name,
                target_dir=target,
                template=args.template,
                force=args.force,
            )
            print(f"Scaffolded new {args.template} project '{args.name}' into {dest}")
            print(f"To run offline: cd {dest} && python app.py")
            print(f"To run tests:   cd {dest} && pytest")
        except (FileExistsError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
