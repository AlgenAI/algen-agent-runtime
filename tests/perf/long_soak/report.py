"""Summarise a long soak's output directory as Markdown tables (for docs/operations.md).

    python tests/perf/long_soak/report.py OUT_DIR [--every 1800] [--compact FILE]

``--every`` sets the row interval of the over-time tables (seconds). ``--compact`` writes a
compact JSON-lines copy of the windows and samples (one per ``--every``) for keeping in the repo.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def slope_per_hour(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of (seconds, value), per hour."""
    if len(points) < 3:
        return float("nan")
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    var = sum((x - mean_x) ** 2 for x, _ in points)
    if not var:
        return float("nan")
    cov = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return cov / var * 3600


def fmt(value: Any, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def mean(samples: list[dict[str, Any]], key: str) -> float:
    values = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
    return sum(values) / len(values) if values else float("nan")


def over_time(windows: list[dict[str, Any]], samples: list[dict[str, Any]], every: float) -> str:
    rows = [
        "| Hour | Ops/s | Runs followed p50 / p95 / p99 (ms) | Turn p50 / p95 (ms) | Errors | "
        "RSS (MB) | FDs | Tasks | PG conns | DB (MB) | events rows | Dead tuples (runs) | "
        "Redis (MB) | Runtime CPU % | Host load |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if not windows:
        return "\n".join(rows)
    end = windows[-1]["t_s"] + windows[-1]["seconds"]
    start = 0.0
    while start < end:
        stop = start + every
        chunk = [w for w in windows if start <= w["t_s"] < stop]
        sample = [s for s in samples if start <= s["t_s"] < stop and "vmrss_mb" in s]
        if not chunk:
            start = stop
            continue
        seconds = sum(w["seconds"] for w in chunk)
        ops = sum(sum(w["ok"].values()) for w in chunk)
        errors = sum(sum(w["errors"].values()) for w in chunk)

        def weighted(op: str, key: str, chunk: list[dict[str, Any]] = chunk) -> float:
            values = [
                (w["latency"][op][key], w["latency"][op]["n"])
                for w in chunk
                if op in w["latency"] and w["latency"][op]["n"]
            ]
            total = sum(n for _, n in values)
            return sum(v * n for v, n in values) / total if total else float("nan")

        last = sample[-1] if sample else {}
        conns = sum(last.get("pg_connections", {}).values()) if last else None
        tables = last.get("tables", {})
        rows.append(
            "| "
            + " | ".join(
                [
                    f"{start / 3600:.1f}",
                    fmt(ops / seconds if seconds else float("nan"), 1),
                    " / ".join(
                        fmt(weighted("run.reader", key), 0)
                        for key in ("p50_ms", "p95_ms", "p99_ms")
                    ),
                    " / ".join(fmt(weighted("turn", key), 0) for key in ("p50_ms", "p95_ms")),
                    fmt(errors),
                    fmt(last.get("vmrss_mb")),
                    fmt(last.get("open_fds")),
                    fmt(last.get("asyncio_tasks")),
                    fmt(conns),
                    fmt(last.get("pg_db_mb")),
                    fmt(tables.get("events", [None])[0]),
                    fmt(tables.get("runs", [None, None])[1]),
                    fmt(last.get("redis_used_mb"), 2),
                    fmt(mean(sample, "process_cpu_percent"), 0),
                    fmt(mean(sample, "host_load1"), 0),
                ]
            )
            + " |"
        )
        start = stop
    return "\n".join(rows)


def leaks(samples: list[dict[str, Any]], restarts: list[dict[str, Any]]) -> str:
    """Per process lifetime (between restarts): first and last RSS, FDs and tasks after a
    10-minute warm-up, and the fitted slope."""
    lifetimes: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        if "process_started" in sample:
            lifetimes.setdefault(sample["process_started"], []).append(sample)
    rows = [
        "| Process lifetime | Minutes | RSS first → last (MB) | RSS slope (MB/h) | FDs first → last "
        "| Tasks first → last | Tasks slope (/h) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, (_, items) in enumerate(sorted(lifetimes.items())):
        if len(items) < 2:
            continue
        base = items[0]["t_s"]
        warm = [s for s in items if s["t_s"] - base >= 600] or items
        minutes = (items[-1]["t_s"] - base) / 60
        rss = [(s["t_s"], s["vmrss_mb"]) for s in warm if "vmrss_mb" in s]
        tasks = [(s["t_s"], s["asyncio_tasks"]) for s in warm if s.get("asyncio_tasks") is not None]
        rows.append(
            "| "
            + " | ".join(
                [
                    str(index + 1),
                    fmt(minutes, 0),
                    f"{fmt(warm[0].get('vmrss_mb'))} → {fmt(warm[-1].get('vmrss_mb'))}",
                    fmt(slope_per_hour(rss), 1),
                    f"{fmt(warm[0].get('open_fds'))} → {fmt(warm[-1].get('open_fds'))}",
                    f"{fmt(warm[0].get('asyncio_tasks'))} → {fmt(warm[-1].get('asyncio_tasks'))}",
                    fmt(slope_per_hour(tasks), 1),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def latency_table(summary: dict[str, Any]) -> str:
    rows = [
        "| Operation | Count | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) | Quiet p95 | Noisy p95 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for op, values in summary.get("latency", {}).items():
        quiet = summary.get("latency_quiet", {}).get(op, {})
        noisy = summary.get("latency_noisy", {}).get(op, {})
        rows.append(
            f"| `{op}` | {fmt(values['n'])} | {fmt(values['p50_ms'], 0)} | "
            f"{fmt(values['p95_ms'], 0)} | {fmt(values['p99_ms'], 0)} | "
            f"{fmt(values['max_ms'], 0)} | {fmt(quiet.get('p95_ms'), 0)} | "
            f"{fmt(noisy.get('p95_ms'), 0)} |"
        )
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--every", type=float, default=1800)
    parser.add_argument("--compact")
    args = parser.parse_args()
    out = Path(args.out)
    windows = load(out / "windows.jsonl")
    samples = load(out / "samples.jsonl")
    chaos = load(out / "chaos.jsonl")
    summary = (
        json.loads((out / "summary.json").read_text()) if (out / "summary.json").exists() else {}
    )
    print("## Over time\n")
    print(over_time(windows, samples, args.every))
    print("\n## Latency (whole soak)\n")
    print(latency_table(summary))
    print("\n## Process lifetimes\n")
    print(leaks(samples, chaos))
    print("\n## Chaos\n")
    for entry in chaos:
        print("- " + json.dumps(entry))
    print("\n## Errors\n")
    for key, value in summary.get("errors", {}).items():
        print(f"- {key}: {value}")
    print("\n## Outcomes\n")
    for key, value in summary.get("outcomes", {}).items():
        print(f"- {key}: {value}")
    print("\n## Noisy\n")
    print(json.dumps(summary.get("noisy", {})))
    print("\n## Invariants\n")
    print(json.dumps(summary.get("invariants", {}), indent=1))
    if args.compact:
        with open(args.compact, "w", encoding="utf-8") as handle:
            for kind, items in (("window", windows), ("sample", samples)):
                step = max(
                    1,
                    round(args.every / (items[1]["t_s"] - items[0]["t_s"]))
                    if len(items) > 1
                    else 1,
                )
                for item in items[::step]:
                    handle.write(json.dumps({"kind": kind, **item}, separators=(",", ":")) + "\n")
            for entry in chaos:
                handle.write(json.dumps({"kind": "chaos", **entry}, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
