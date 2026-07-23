#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT.parent
WORKER = SCRIPT_DIR / "run_benchmark_worker.py"
MODES = ("vhost-user-blk-shared-cache", "lazy-pmem")
BACKINGS = ("file", "memfd")
CELLS = tuple((mode, backing) for mode in MODES for backing in BACKINGS)
MODE_TAGS = {"vhost-user-blk-shared-cache": "s", "lazy-pmem": "p"}
BACKING_TAGS = {"file": "f", "memfd": "m"}
CELL_TAGS = {
    "sf": ("vhost-user-blk-shared-cache", "file"),
    "sm": ("vhost-user-blk-shared-cache", "memfd"),
    "pf": ("lazy-pmem", "file"),
    "pm": ("lazy-pmem", "memfd"),
}
MIB = 1024 * 1024


def write_json_atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cell_order(
    round_number: int,
    limit_index: int,
    cells: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    offset = (round_number + limit_index - 1) % len(cells)
    return cells[offset:] + cells[:offset]


def cleanup_taps(vm_count: int) -> None:
    for index in range(vm_count):
        subprocess.run(
            ["ip", "link", "delete", f"kpmb{index}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def parse_properties(contents: str) -> dict[str, str]:
    values = {}
    for line in contents.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def systemd_properties(unit: str) -> dict[str, str]:
    process = subprocess.run(
        [
            "systemctl",
            "show",
            unit + ".service",
            "--property=Result",
            "--property=ExecMainCode",
            "--property=ExecMainStatus",
            "--property=MemoryPeak",
            "--property=CPUUsageNSec",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return parse_properties(process.stdout) if process.returncode == 0 else {}


def classify(
    returncode: int, output_exists: bool, properties: dict[str, str], log: str
) -> str:
    if returncode == 0 and output_exists:
        return "pass"
    result = properties.get("Result", "").lower()
    lowered = log.lower()
    if "oom" in result or "oom-kill" in lowered or "out of memory" in lowered:
        return "memory-limit"
    return "runtime-failure"


def worker_command(
    *,
    root: Path,
    output: Path,
    unit: str,
    round_number: int,
    execution_order: int,
    mode: str,
    backing: str,
    vm_count: int,
    memory_max_mib: int,
) -> list[str]:
    return [
        "systemd-run",
        "--wait",
        "--pipe",
        "--quiet",
        f"--unit={unit}",
        "--property=MemoryAccounting=yes",
        "--property=CPUAccounting=yes",
        "--property=IOAccounting=yes",
        f"--property=MemoryMax={memory_max_mib * MIB}",
        "--property=MemorySwapMax=0",
        sys.executable,
        str(WORKER),
        "--root",
        str(root),
        "--output",
        str(output),
        "--round",
        str(round_number),
        "--execution-order",
        str(execution_order),
        "--mode",
        mode,
        "--cache-backing",
        backing,
        "--source-state",
        "plaintext-warm",
        "--vm-count",
        str(vm_count),
        "--workload",
        "full-tree-scan",
    ]


def valid_existing(path: Path, contract: dict[str, object]) -> bool:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return all(row.get(key) == value for key, value in contract.items())


def validate_rows(
    rows: list[dict],
    *,
    rounds: int,
    limits: list[int],
    vm_count: int,
    cells: tuple[tuple[str, str], ...],
) -> None:
    expected = {
        (round_number, limit, mode, backing)
        for round_number in range(1, rounds + 1)
        for limit in limits
        for mode, backing in cells
    }
    actual = {
        (
            int(row["round"]),
            int(row["memory_max_mib"]),
            row["mode"],
            row["cache_backing"],
        )
        for row in rows
    }
    if len(rows) != len(actual) or actual != expected:
        raise ValueError("pressure sample matrix is incomplete or duplicated")
    if any(int(row["vm_count"]) != vm_count for row in rows):
        raise ValueError("pressure sample has unexpected VM count")
    allowed = {"pass", "memory-limit", "runtime-failure"}
    if any(row["classification"] not in allowed for row in rows):
        raise ValueError("pressure sample has unknown classification")


def write_report(output_dir: Path, rows: list[dict], vm_count: int) -> None:
    groups: dict[tuple[int, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[
            (int(row["memory_max_mib"]), row["mode"], row["cache_backing"])
        ].append(row)
    lines = [
        "# Cache backing under cgroup memory pressure",
        "",
        (
            f"Each sample runs {vm_count} VMs, prewarms and scans the same 165.95 MiB "
            "EROFS tree, with `MemorySwapMax=0`."
        ),
        "",
        "| MemoryMax (MiB) | Transport | Backing | Passes | OOM | Runtime failures | Pass rate | Successful MemoryPeak p50 (MiB) |",
        "|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "vhost-user-blk-shared-cache": "Shared BLK",
        "lazy-pmem": "Lazy PMEM",
    }
    summary = []
    for key, samples in sorted(groups.items()):
        limit, mode, backing = key
        passes = sum(row["classification"] == "pass" for row in samples)
        oom = sum(row["classification"] == "memory-limit" for row in samples)
        runtime = sum(row["classification"] == "runtime-failure" for row in samples)
        peaks = []
        for row in samples:
            if row["classification"] != "pass":
                continue
            systemd_peak = str(row["systemd"].get("MemoryPeak", ""))
            if systemd_peak.isdigit():
                peaks.append(int(systemd_peak) / MIB)
                continue
            worker_path = row.get("worker_result_path")
            if worker_path:
                worker = json.loads(Path(worker_path).read_text(encoding="utf-8"))
                peaks.append(
                    int(worker["metrics"]["held_memory_peak_bytes"]) / MIB
                )
        peak_p50 = (
            (peaks[(len(peaks) - 1) // 2] + peaks[len(peaks) // 2]) / 2
            if peaks
            else None
        )
        item = {
            "memory_max_mib": limit,
            "mode": mode,
            "cache_backing": backing,
            "samples": len(samples),
            "passes": passes,
            "memory_limit_failures": oom,
            "runtime_failures": runtime,
            "pass_rate_percent": passes / len(samples) * 100,
            "successful_memory_peak_mib_p50": peak_p50,
        }
        summary.append(item)
        peak_text = f"{peak_p50:.1f}" if peak_p50 is not None else "-"
        lines.append(
            f"| {limit} | {labels[mode]} | {backing} | {passes} | {oom} | "
            f"{runtime} | {item['pass_rate_percent']:.0f}% | {peak_text} |"
        )
    lines.append("")
    write_json_atomic(output_dir / "pressure_summary.json", summary)
    (output_dir / "pressure_report.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    worker_results = output_dir / "worker-results"
    logs = output_dir / "worker-logs"
    worker_results.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    selected_cells = tuple(CELL_TAGS[tag] for tag in args.cells)
    contract = {
        "schema_version": 1,
        "rounds": args.rounds,
        "vm_count": args.vm_count,
        "memory_max_mib": args.memory_max_mib,
        "memory_swap_max_bytes": 0,
        "modes": list(MODES),
        "cache_backings": list(BACKINGS),
        "cells": list(args.cells),
        "workload": "full-tree-scan",
        "worker_sha256": sha256_file(WORKER),
        "binary_sha256": {
            path.name: sha256_file(path)
            for path in sorted((root / "bin").iterdir())
            if path.is_file()
        },
    }
    manifest = output_dir / "run-manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_text())["contract"] != contract:
            raise RuntimeError("existing pressure run uses a different contract")
    else:
        write_json_atomic(
            manifest,
            {
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "contract": contract,
            },
        )

    rows = []
    for round_number in range(1, args.rounds + 1):
        for limit_index, limit in enumerate(args.memory_max_mib):
            for execution_order, (mode, backing) in enumerate(
                cell_order(round_number, limit_index, selected_cells), start=1
            ):
                stem = (
                    f"r{round_number:02d}-m{limit}-{MODE_TAGS[mode]}"
                    f"{BACKING_TAGS[backing]}"
                )
                row_path = output_dir / f"{stem}.json"
                sample_contract = {
                    "round": round_number,
                    "memory_max_mib": limit,
                    "mode": mode,
                    "cache_backing": backing,
                    "vm_count": args.vm_count,
                }
                if valid_existing(row_path, sample_contract):
                    rows.append(json.loads(row_path.read_text()))
                    print(f"SKIP {stem}", flush=True)
                    continue
                worker_output = worker_results / f"{stem}.json"
                log_path = logs / f"{stem}.log"
                unit = f"klp-q-{stem}"
                worker_output.unlink(missing_ok=True)
                cleanup_taps(args.vm_count)
                print(f"RUN  {stem}", flush=True)
                try:
                    with log_path.open("wb") as destination:
                        process = subprocess.run(
                            worker_command(
                                root=root,
                                output=worker_output,
                                unit=unit,
                                round_number=round_number,
                                execution_order=execution_order,
                                mode=mode,
                                backing=backing,
                                vm_count=args.vm_count,
                                memory_max_mib=limit,
                            ),
                            stdout=destination,
                            stderr=subprocess.STDOUT,
                            check=False,
                        )
                finally:
                    cleanup_taps(args.vm_count)
                properties = systemd_properties(unit)
                log = log_path.read_text(encoding="utf-8", errors="replace")
                classification = classify(
                    process.returncode, worker_output.is_file(), properties, log
                )
                row = {
                    **sample_contract,
                    "execution_order": execution_order,
                    "unit": unit,
                    "returncode": process.returncode,
                    "classification": classification,
                    "systemd": properties,
                    "worker_result_path": (
                        str(worker_output) if worker_output.is_file() else None
                    ),
                    "log": str(log_path),
                }
                write_json_atomic(row_path, row)
                rows.append(row)
                subprocess.run(
                    ["systemctl", "reset-failed", unit + ".service"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"DONE {stem} {classification}", flush=True)

    validate_rows(
        rows,
        rounds=args.rounds,
        limits=args.memory_max_mib,
        vm_count=args.vm_count,
        cells=selected_cells,
    )
    write_report(output_dir, rows, args.vm_count)
    snapshot = output_dir / "source-snapshot"
    snapshot.mkdir(exist_ok=True)
    for path in (WORKER, SCRIPT):
        shutil.copy2(path, snapshot / path.name)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--vm-count", type=int, default=8)
    parser.add_argument(
        "--memory-max-mib", type=int, nargs="+", default=[960, 1024, 1536, 2048, 2560]
    )
    parser.add_argument(
        "--cells",
        nargs="+",
        choices=tuple(CELL_TAGS),
        default=list(CELL_TAGS),
    )
    args = parser.parse_args()
    if (
        args.rounds <= 0
        or args.vm_count <= 0
        or any(limit <= 0 for limit in args.memory_max_mib)
    ):
        parser.error("rounds, VM count, and limits must be positive")
    if len(set(args.memory_max_mib)) != len(args.memory_max_mib):
        parser.error("memory limits must be unique")
    if len(set(args.cells)) != len(args.cells):
        parser.error("cells must be unique")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
