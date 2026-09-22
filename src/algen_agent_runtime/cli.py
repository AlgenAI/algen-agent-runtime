"""CLI entry point for Algen Agent Runtime."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from algen_agent_runtime.scaffold import AVAILABLE_TEMPLATES, scaffold_project


def _run_serve(
    host: str | None = None,
    port: int | None = None,
    config: list[str] | None = None,
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
    bind_host = host or settings.api.host
    bind_port = port if port is not None else settings.api.port

    uvicorn.run(create_app(settings), host=bind_host, port=bind_port)


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
        _run_serve(host=args.host, port=args.port, config=args.config)
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
