#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from run_benchmark_worker import CAPTURE_ORDER, validate_worker_result


MODE_BLK_SHARED = "vhost-user-blk-shared-cache"
MODE_PMEM = "lazy-pmem"
MODES = (MODE_BLK_SHARED, MODE_PMEM)
CACHE_BACKINGS = ("file", "memfd")
CELLS = tuple((mode, backing) for mode in MODES for backing in CACHE_BACKINGS)
SOURCE_STATES = ("plaintext-cold", "plaintext-warm")
MATERIALIZATION_MAX_BYTES = 1024 * 1024
MODE_TAGS = {MODE_BLK_SHARED: "s", MODE_PMEM: "p"}
BACKING_TAGS = {"file": "f", "memfd": "m"}
STATE_TAGS = {"plaintext-cold": "c", "plaintext-warm": "w"}
CELL_PERMUTATIONS = (
    CELLS,
    (CELLS[1], CELLS[2], CELLS[3], CELLS[0]),
    (CELLS[2], CELLS[3], CELLS[0], CELLS[1]),
    (CELLS[3], CELLS[0], CELLS[1], CELLS[2]),
    tuple(reversed(CELLS)),
    (CELLS[2], CELLS[1], CELLS[0], CELLS[3]),
)
SCRIPT_DIR = Path(__file__).resolve().parent
WORKER = SCRIPT_DIR / "run_benchmark_worker.py"


def cell_order(round_number: int, cell_index: int) -> tuple[tuple[str, str], ...]:
    if round_number <= 0 or cell_index < 0:
        raise ValueError("round_number must be positive and cell_index non-negative")
    return CELL_PERMUTATIONS[(round_number - 1 + cell_index) % len(CELL_PERMUTATIONS)]


def expected_result_contract(
    *,
    round_number: int,
    execution_order: int,
    mode: str,
    cache_backing: str,
    source_state: str,
    vm_count: int,
) -> dict[str, object]:
    return {
        "round": round_number,
        "execution_order": execution_order,
        "mode": mode,
        "cache_backing": cache_backing,
        "source_state": source_state,
        "vm_count": vm_count,
    }


def completed_sample(path: Path, contract: dict[str, object]) -> bool:
    if not path.is_file():
        return False
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        validate_worker_result(result)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return all(result.get(name) == value for name, value in contract.items())


def worker_command(
    *,
    root: Path,
    output: Path,
    unit: str,
    round_number: int,
    execution_order: int,
    mode: str,
    cache_backing: str,
    source_state: str,
    vm_count: int,
) -> list[str]:
    return [
        "systemd-run",
        "--quiet",
        "--collect",
        "--wait",
        "--pipe",
        f"--unit={unit}",
        "--property=MemoryAccounting=yes",
        "--property=CPUAccounting=yes",
        "--property=IOAccounting=yes",
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
        cache_backing,
        "--source-state",
        source_state,
        "--vm-count",
        str(vm_count),
    ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_revision(path: Path) -> dict[str, object]:
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "-C", str(path), "diff", "--binary", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {
        "head": head,
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "status": status,
    }


def build_run_contract(root: Path, rounds: int, vm_counts: list[int]) -> dict[str, object]:
    binaries = {
        path.name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted((root / "bin").iterdir())
        if path.is_file()
    }
    scripts = {
        path.name: sha256_file(path)
        for path in sorted(SCRIPT_DIR.glob("*.py"))
        if path.name in {
            "benchmark_metrics.py",
            "benchmark_workloads.py",
            "run_cache_backing_evidence.py",
            "run_benchmark_worker.py",
            "run_reuse_benchmark.py",
        }
    }
    repositories = {
        "accelerator": source_revision(Path("/root/virtiolazyd/worktrees/accelerator-benchmark")),
        "lazyd": source_revision(
            Path("/root/virtiolazyd/worktrees/lazyd-backing-benchmark")
        ),
        "cloud-hypervisor": source_revision(Path("/root/virtiolazyd/worktrees/ch-benchmark")),
        "sandboxer": source_revision(Path("/root/virtiolazyd/worktrees/sandboxer-benchmark")),
    }
    return {
        "schema_version": 1,
        "rounds": rounds,
        "vm_counts": vm_counts,
        "source_states": list(SOURCE_STATES),
        "modes": list(MODES),
        "cache_backings": list(CACHE_BACKINGS),
        "workload": "nginx-first-request",
        "materialization_max_bytes": MATERIALIZATION_MAX_BYTES,
        "capture_order": list(CAPTURE_ORDER),
        "binary_artifacts": binaries,
        "script_sha256": scripts,
        "images_tsv_sha256": sha256_file(root / "images.tsv"),
        "repositories": repositories,
        "host": {
            "platform": platform.platform(),
            "kernel": platform.release(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").exists(),
            "kvm": Path("/dev/kvm").exists(),
        },
    }


def write_json_atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    logs_dir = output_dir / "worker-logs"
    raw_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    contract = build_run_contract(root, args.rounds, args.vm_counts)
    manifest_path = output_dir / "run-manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("contract") != contract:
            raise RuntimeError("existing run manifest does not match requested benchmark contract")
    else:
        write_json_atomic(
            manifest_path,
            {
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "contract": contract,
            },
        )

    failures: list[dict[str, object]] = []
    completed = skipped = 0
    cell_index = 0
    for round_number in range(1, args.rounds + 1):
        for source_state in SOURCE_STATES:
            for vm_count in args.vm_counts:
                for execution_order, (mode, cache_backing) in enumerate(
                    cell_order(round_number, cell_index), start=1
                ):
                    stem = (
                        f"r{round_number:02d}-{STATE_TAGS[source_state]}-{vm_count}-"
                        f"{MODE_TAGS[mode]}{BACKING_TAGS[cache_backing]}"
                    )
                    output = raw_dir / f"{stem}.json"
                    contract_fields = expected_result_contract(
                        round_number=round_number,
                        execution_order=execution_order,
                        mode=mode,
                        cache_backing=cache_backing,
                        source_state=source_state,
                        vm_count=vm_count,
                    )
                    if completed_sample(output, contract_fields):
                        skipped += 1
                        print(f"SKIP {stem}", flush=True)
                        continue
                    unit = f"klp-e-{stem}"
                    command = worker_command(
                        root=root,
                        output=output,
                        unit=unit,
                        round_number=round_number,
                        execution_order=execution_order,
                        mode=mode,
                        cache_backing=cache_backing,
                        source_state=source_state,
                        vm_count=vm_count,
                    )
                    log_path = logs_dir / f"{stem}.log"
                    print(f"RUN  {stem}", flush=True)
                    with log_path.open("wb") as log:
                        process = subprocess.run(
                            command,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            check=False,
                        )
                    if process.returncode != 0 or not completed_sample(output, contract_fields):
                        failure = {
                            **contract_fields,
                            "returncode": process.returncode,
                            "log": str(log_path),
                        }
                        failures.append(failure)
                        print(f"FAIL {stem} rc={process.returncode}", flush=True)
                        if args.stop_on_error:
                            write_json_atomic(
                                output_dir / "run-status.json",
                                {"completed": completed, "skipped": skipped, "failures": failures},
                            )
                            return 1
                    else:
                        completed += 1
                        print(f"DONE {stem}", flush=True)
                    write_json_atomic(
                        output_dir / "run-status.json",
                        {"completed": completed, "skipped": skipped, "failures": failures},
                    )
                cell_index += 1
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--vm-counts", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--stop-on-error", action="store_true")
    args = parser.parse_args()
    if args.rounds <= 0 or any(value <= 0 for value in args.vm_counts):
        parser.error("rounds and vm-counts must be positive")
    if len(set(args.vm_counts)) != len(args.vm_counts):
        parser.error("vm-counts must be unique")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
