"""CLI entry point for Algen Agent Runtime."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from algen_agent_runtime.scaffold import AVAILABLE_TEMPLATES, scaffold_project

if TYPE_CHECKING:
    from algen_agent_runtime.config.settings import AppSettings


def _settings_from_config(config: list[str] | None) -> AppSettings:
    from algen_agent_runtime.config.settings import AppSettings, load_settings

    config_files: list[str] = []
    if config:
        config_files.extend(config)
    else:
        env_config = os.getenv("ALGEN_AGENT_RUNTIME_CONFIG", "")
        if env_config:
            config_files.extend(filter(None, env_config.split(os.pathsep)))
    return (
        load_settings(tuple(Path(item) for item in config_files)) if config_files else AppSettings()
    )


def _run_maintenance(config: list[str] | None = None) -> int:
    import asyncio

    from algen_agent_runtime.orchestration.container import build_container

    container = build_container(_settings_from_config(config))
    maintenance = container.maintenance
    if maintenance is None:
        raise RuntimeError("runtime container has no maintenance service")

    async def sweep() -> int:
        # Run recovery is deliberately not started: this process only performs housekeeping.
        try:
            report = await maintenance.run_once()
        finally:
            await container.aclose()
        print(report.model_dump_json(indent=2))
        return 1 if report.errors else 0

    return asyncio.run(sweep())


def _run_serve(
    host: str | None = None,
    port: int | None = None,
    config: list[str] | None = None,
    agent_ui: bool | None = None,
) -> None:
    import uvicorn

    from algen_agent_runtime.api.app import create_app

    settings = _settings_from_config(config)

    if agent_ui is not None:
        settings = settings.model_copy(
            update={"api": settings.api.model_copy(update={"agent_ui_enabled": agent_ui})}
        )

    bind_host = host or settings.api.host
    bind_port = port if port is not None else settings.api.port

    if settings.api.agent_ui_enabled:
        print(f"Agent UI enabled at: http://{bind_host}:{bind_port}{settings.api.agent_ui_path}")

    uvicorn.run(
        create_app(settings),
        host=bind_host,
        port=bind_port,
        # The trusted-proxy identity hook checks the direct peer; never let X-Forwarded-For
        # rewrite it.
        proxy_headers=not settings.api.trusted_proxy_identity.enabled,
    )


def _discover_config_paths(config: list[str] | None) -> tuple[Path, ...] | None:
    """Explicit paths, else ALGEN_AGENT_RUNTIME_CONFIG, else the first conventional file found."""
    if config:
        return tuple(Path(item) for item in config)
    env_config = os.getenv("ALGEN_AGENT_RUNTIME_CONFIG", "")
    if env_config:
        return tuple(Path(item) for item in filter(None, env_config.split(os.pathsep)))
    candidates = [
        Path("agent.yaml"),
        Path("config/agent.yaml"),
        Path("algen-agent-runtime.yaml"),
    ]
    found = next((c for c in candidates if c.exists()), None)
    return (found,) if found else None


def _run_describe(
    config: list[str] | None = None,
    paths: list[str] | None = None,
    as_json: bool = False,
) -> int:
    from algen_agent_runtime.config.settings import AppSettings, load_settings
    from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError
    from algen_agent_runtime.orchestration.describe import describe_components

    for path in reversed(paths or []):
        sys.path.insert(0, str(Path(path).resolve()))
    config_paths = _discover_config_paths(config)
    try:
        settings = load_settings(config_paths) if config_paths else AppSettings()
        inventory = describe_components(settings)
    except (ConfigurationError, ConflictError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(inventory.model_dump_json(indent=2))
        return 0
    for plugin in inventory.plugins:
        print(f"plugin {plugin.reference} ({plugin.name} {plugin.version})")
    for tool in inventory.tools:
        print(f"tool {tool.name} [{tool.side_effect}] from {tool.source}")
    for kind in ("planners", "response_composers", "verifiers", "context_builders"):
        for component in getattr(inventory, kind):
            print(f"{kind.removesuffix('s')} {component.name} from {component.source}")
    for provider in inventory.providers:
        print(f"provider {provider.name} from {provider.source}")
    for server in inventory.mcp_servers:
        auth = f" auth=oauth({server.auth.token_scope})" if server.auth else ""
        print(f"mcp_server {server.name} from {server.source}{auth}")
    for kind in ("semantic_layers", "analytical_methods", "model_services"):
        for component in getattr(inventory, kind):
            print(f"{kind.removesuffix('s')} {component.name} from {component.source}")
    if inventory.environment:
        print("environment " + ", ".join(inventory.environment))
    return 0


def _run_check(
    config: list[str] | None = None,
    paths: list[str] | None = None,
    project_root: str = ".",
    as_json: bool = False,
    strict: bool = False,
) -> int:
    from algen_agent_runtime.orchestration.check import check_project

    for path in reversed(paths or []):
        sys.path.insert(0, str(Path(path).resolve()))
    config_paths = _discover_config_paths(config)
    if not config_paths:
        print("Error: no configuration found; pass agent.yaml", file=sys.stderr)
        return 1
    report = check_project(config_paths, project_root=Path(project_root))
    if as_json:
        print(report.model_dump_json(indent=2))
    else:
        for issue in report.issues:
            where = f" {issue.location}" if issue.location else ""
            print(f"{issue.severity} {issue.code}{where}: {issue.message}")
        print(f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 0 if report.ok and not (strict and report.warnings) else 1


def _run_generate_ui(
    config: list[str] | None = None,
    output: str = "agent-ui.html",
    title: str | None = None,
    open_browser: bool = False,
) -> None:
    from algen_agent_runtime.config.settings import AppSettings, load_settings
    from algen_agent_runtime.ui.generator import generate_agent_ui_html

    config_paths = _discover_config_paths(config)
    raw_yaml: str | None = None

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


def _run_policy(args: argparse.Namespace) -> int:
    """Generate a signing key, or sign or verify a policy bundle. Keys come from references."""
    import json

    from algen_agent_runtime.exceptions.errors import ConfigurationError
    from algen_agent_runtime.policies import bundles

    try:
        if args.policy_command == "keygen":
            private_path, public_path = Path(args.private_key), Path(args.public_key)
            for path in (private_path, public_path):
                if path.exists():
                    raise ConfigurationError(f"{path} already exists; not overwriting a key")
            private_pem, public_pem = bundles.generate_signing_key()
            # The private key is created readable by its owner only.
            descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(private_pem)
            public_path.write_text(public_pem, encoding="utf-8")
            print(f"Wrote private key {private_path} (keep it secret) and public key {public_path}")
            return 0
        if args.policy_command == "sign":
            data = Path(args.bundle).read_bytes()
            key = bundles.resolve_key_reference(args.key, name="signing key")
            signature = bundles.sign_policy_bundle(data, key)
            output = Path(args.output or args.bundle + bundles.SIGNATURE_SUFFIX)
            output.write_text(signature + "\n", encoding="ascii")
            print(f"Wrote signature {output}")
            return 0
        loaded = bundles.load_policy_bundle(
            args.bundle,
            signature=args.signature,
            trusted_keys=tuple(args.trusted_key or ()),
            require_signed=True,
            min_version=args.min_version,
        )
    except (ConfigurationError, OSError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "name": loaded.bundle.name,
                "version": loaded.bundle.version,
                "issued_at": loaded.bundle.issued_at.isoformat(),
                "expires_at": (
                    loaded.bundle.expires_at.isoformat() if loaded.bundle.expires_at else None
                ),
                "sha256": loaded.sha256,
                "key_id": loaded.key_id,
            },
            indent=2,
        )
    )
    return 0


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

    # maintenance command
    maintenance_parser = subparsers.add_parser(
        "maintenance",
        help="Run one housekeeping sweep (paused-run expiry and retention) and exit",
    )
    maintenance_parser.add_argument(
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

    # describe command
    describe_parser = subparsers.add_parser(
        "describe",
        help="List every component the settings would register, without running any of them",
    )
    describe_parser.add_argument(
        "--config",
        "-c",
        nargs="*",
        default=None,
        help="Path(s) to configuration YAML file(s) (defaults to agent.yaml or config/agent.yaml)",
    )
    describe_parser.add_argument(
        "--path",
        action="append",
        default=None,
        help="Directory to import plugin modules from, such as src (repeatable)",
    )
    describe_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the inventory as JSON",
    )
    # check command
    check_parser = subparsers.add_parser(
        "check",
        help="Validate a project as Studio will, and warn about code Studio cannot run",
    )
    check_parser.add_argument(
        "config",
        nargs="*",
        default=None,
        help="Configuration YAML file(s) (defaults to agent.yaml or config/agent.yaml)",
    )
    check_parser.add_argument(
        "--path",
        action="append",
        default=None,
        help="Directory to import plugin modules from, such as src (repeatable)",
    )
    check_parser.add_argument(
        "--project-root",
        default=".",
        help="Directory whose Python code is scanned (default: current directory)",
    )
    check_parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    check_parser.add_argument(
        "--strict", action="store_true", help="Exit with status 1 on warnings too"
    )
    # policy command
    policy_parser = subparsers.add_parser(
        "policy", help="Create signing keys, and sign or verify policy bundles"
    )
    policy_commands = policy_parser.add_subparsers(dest="policy_command", required=True)
    keygen_parser = policy_commands.add_parser("keygen", help="Generate an Ed25519 key pair")
    keygen_parser.add_argument("--private-key", required=True, help="Private key PEM to create")
    keygen_parser.add_argument("--public-key", required=True, help="Public key PEM to create")
    sign_parser = policy_commands.add_parser("sign", help="Write a bundle's detached signature")
    sign_parser.add_argument("bundle", help="Policy bundle YAML or JSON file")
    sign_parser.add_argument(
        "--key", required=True, help="Private key reference: env://VARIABLE or file://path"
    )
    sign_parser.add_argument("--output", default=None, help="Signature file (default <bundle>.sig)")
    verify_parser = policy_commands.add_parser(
        "verify", help="Verify a bundle as the Runtime does at start-up (signature required)"
    )
    verify_parser.add_argument("bundle", help="Policy bundle YAML or JSON file")
    verify_parser.add_argument("--signature", default=None, help="Signature file")
    verify_parser.add_argument(
        "--trusted-key",
        action="append",
        required=True,
        help="Public key reference: env://VARIABLE or file://path (repeatable)",
    )
    verify_parser.add_argument("--min-version", type=int, default=None)
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
    elif args.command == "maintenance":
        sys.exit(_run_maintenance(config=args.config))
    elif args.command == "check":
        sys.exit(
            _run_check(
                config=args.config or None,
                paths=args.path,
                project_root=args.project_root,
                as_json=args.json,
                strict=args.strict,
            )
        )
    elif args.command == "describe":
        sys.exit(_run_describe(config=args.config, paths=args.path, as_json=args.json))
    elif args.command == "policy":
        sys.exit(_run_policy(args))
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
