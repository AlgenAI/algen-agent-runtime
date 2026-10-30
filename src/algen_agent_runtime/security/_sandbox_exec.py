"""Sandbox launcher: apply resource limits, scrub the environment, then exec the command.

The Runtime runs this file as ``python -I -S _sandbox_exec.py <spec> -- argv...`` so that it
imports nothing outside the standard library. ``spec`` is JSON with ``limits`` (resource
name to value), ``env`` (the variable names to keep) and ``cwd``; it holds no secrets. The
limits are inherited by everything ``argv`` starts, including a wrapper such as ``bwrap``.
POSIX only.
"""

import json
import os
import resource
import sys
from typing import NoReturn

_LIMITS = {
    "cpu_seconds": "RLIMIT_CPU",
    "memory_bytes": "RLIMIT_AS",
    "open_files": "RLIMIT_NOFILE",
    "processes": "RLIMIT_NPROC",
    "file_size_bytes": "RLIMIT_FSIZE",
}


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"algen sandbox: {message}\n")
    sys.stderr.flush()
    os._exit(126)


def main(argv: list[str]) -> None:
    if len(argv) < 4 or argv[2] != "--":
        _fail("usage: _sandbox_exec.py <spec> -- command [args...]")
    spec = json.loads(argv[1])
    command = argv[3:]
    for name, value in (spec.get("limits") or {}).items():
        if value is None:
            continue
        constant = getattr(resource, _LIMITS.get(name, ""), None)
        if constant is None:
            _fail(f"resource limit {name} is not supported on this platform")
        _soft, hard = resource.getrlimit(constant)
        limit = value if hard == resource.RLIM_INFINITY or hard < 0 else min(value, hard)
        try:
            resource.setrlimit(constant, (limit, limit))
        except (ValueError, OSError) as exc:
            _fail(f"could not set {name} to {limit}: {exc}")
    keep = set(spec.get("env") or ())
    environment = {key: value for key, value in os.environ.items() if key in keep}
    cwd = spec.get("cwd")
    if cwd:
        try:
            os.chdir(cwd)
        except OSError as exc:
            _fail(f"could not change to {cwd}: {exc}")
    try:
        os.execvpe(command[0], command, environment)
    except OSError as exc:
        _fail(f"could not start {command[0]}: {exc.strerror}")


if __name__ == "__main__":
    main(sys.argv)
