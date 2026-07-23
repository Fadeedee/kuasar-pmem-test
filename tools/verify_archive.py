#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "harness"
RESULTS = ROOT / "results"
PRESSURE_SAMPLE = re.compile(r"r\d\d-m\d+-(?:pf|pm|sf|sm)\.json")


@dataclass(frozen=True)
class FunctionalRun:
    name: str
    directory: Path
    expected_samples: int
    worker_source: Path


FUNCTIONAL_RUNS = (
    FunctionalRun("three-path", RESULTS / "three-path", 180, HARNESS),
    FunctionalRun(
        "cache-backing",
        RESULTS / "cache-backing",
        240,
        RESULTS / "cache-backing" / "source-snapshot",
    ),
    FunctionalRun("full-tree", RESULTS / "full-tree", 120, HARNESS),
)

PRESSURE_RUNS = (
    ("pmem-pressure", RESULTS / "pmem-pressure", 50),
    ("blk-pressure", RESULTS / "blk-pressure", 18),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_worker(source: Path, module_name: str):
    script = source / "run_benchmark_worker.py"
    sys.path.insert(0, str(source))
    try:
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load worker: {script}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def expected_grid(manifest: dict) -> set[tuple]:
    backings = manifest.get("cache_backings")
    if backings is None:
        backings = [manifest["cache_backing"]]
    return {
        (round_number, state, vm_count, mode, backing)
        for round_number in range(1, int(manifest["rounds"]) + 1)
        for state in manifest["source_states"]
        for vm_count in manifest["vm_counts"]
        for mode in manifest["modes"]
        for backing in backings
    }


def verify_script_hashes(manifest: dict, source: Path) -> None:
    for name, expected in manifest["script_sha256"].items():
        path = source / name
        if not path.is_file():
            raise RuntimeError(f"missing archived script: {path}")
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(
                f"script hash mismatch for {path}: expected {expected}, got {actual}"
            )


def verify_workload(rows: list[dict]) -> None:
    names = {row["workload"]["name"] for row in rows}
    if len(names) != 1:
        raise RuntimeError(f"mixed workloads: {sorted(names)}")
    name = next(iter(names))
    if name == "nginx-first-request":
        hashes = {row["workload"]["response_sha256"] for row in rows}
        sizes = {int(row["workload"]["response_bytes"]) for row in rows}
        if len(hashes) != 1 or len(sizes) != 1 or next(iter(sizes)) <= 0:
            raise RuntimeError("Nginx workload output differs across samples")
        return
    if name == "full-tree-scan":
        byte_counts = set()
        for row in rows:
            values = [int(value) for value in row["workload"]["bytes_per_vm"]]
            if (
                len(values) != int(row["vm_count"])
                or len(set(values)) != 1
                or values[0] <= 0
            ):
                raise RuntimeError("full-tree byte counts are incomplete")
            byte_counts.add(values[0])
        if len(byte_counts) != 1:
            raise RuntimeError("full-tree byte counts differ across samples")
        return
    raise RuntimeError(f"unexpected formal workload: {name}")


def verify_functional(run: FunctionalRun) -> int:
    manifest = json.loads((run.directory / "run-manifest.json").read_text())["contract"]
    status = json.loads((run.directory / "run-status.json").read_text())
    rows = [
        json.loads(path.read_text())
        for path in sorted((run.directory / "raw").glob("*.json"))
    ]
    if len(rows) != run.expected_samples:
        raise RuntimeError(
            f"{run.name}: expected {run.expected_samples} samples, got {len(rows)}"
        )

    worker = load_worker(run.worker_source, "worker_" + run.name.replace("-", "_"))
    for row in rows:
        worker.validate_worker_result(row)

    keys = {
        (
            int(row["round"]),
            row["source_state"],
            int(row["vm_count"]),
            row["mode"],
            row["cache_backing"],
        )
        for row in rows
    }
    if len(keys) != len(rows) or keys != expected_grid(manifest):
        raise RuntimeError(f"{run.name}: sample grid is incomplete or duplicated")
    if status["failures"] or int(status["completed"]) + int(status["skipped"]) != len(rows):
        raise RuntimeError(f"{run.name}: run status does not cover every sample")

    verify_script_hashes(manifest, run.worker_source)
    verify_workload(rows)
    if run.name == "three-path":
        audit = json.loads((run.directory / "analysis" / "audit.json").read_text())
        if int(audit["sample_count"]) != len(rows) or not all(
            audit["invariants"].values()
        ):
            raise RuntimeError("three-path audit contains a failed invariant")
    print(f"{run.name}: {len(rows)} samples PASS")
    return len(rows)


def pressure_rows(directory: Path) -> list[tuple[Path, dict]]:
    return [
        (path, json.loads(path.read_text()))
        for path in sorted(directory.iterdir())
        if path.is_file() and PRESSURE_SAMPLE.fullmatch(path.name)
    ]


def verify_pressure(name: str, directory: Path, expected_samples: int, worker) -> int:
    manifest = json.loads((directory / "run-manifest.json").read_text())["contract"]
    rows = pressure_rows(directory)
    expected = (
        int(manifest["rounds"])
        * len(manifest["memory_max_mib"])
        * len(manifest["cells"])
    )
    if expected != expected_samples or len(rows) != expected_samples:
        raise RuntimeError(
            f"{name}: expected {expected_samples} outcomes, got {len(rows)}"
        )
    keys = {
        (
            int(row["round"]),
            int(row["memory_max_mib"]),
            row["mode"],
            row["cache_backing"],
        )
        for _, row in rows
    }
    if len(keys) != len(rows):
        raise RuntimeError(f"{name}: duplicate pressure outcomes")

    classifications: dict[str, int] = {}
    for path, row in rows:
        classification = row["classification"]
        if classification not in {"pass", "memory-limit", "runtime-failure"}:
            raise RuntimeError(f"{name}: invalid classification in {path.name}")
        classifications[classification] = classifications.get(classification, 0) + 1
        if classification == "pass":
            worker_result = directory / "worker-results" / path.name
            worker.validate_worker_result(json.loads(worker_result.read_text()))

    worker_hash = sha256_file(
        directory / "source-snapshot" / "run_benchmark_worker.py"
    )
    if worker_hash != manifest["worker_sha256"]:
        raise RuntimeError(f"{name}: pressure worker hash mismatch")
    rendered = ", ".join(
        f"{classification}={count}"
        for classification, count in sorted(classifications.items())
    )
    print(f"{name}: {len(rows)} outcomes PASS ({rendered})")
    return len(rows)


def run_analysis(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"analysis command failed: {command}\n{completed.stdout}\n{completed.stderr}"
        )


def compare_files(reference: Path, generated: Path, names: tuple[str, ...]) -> None:
    for name in names:
        left = reference / name
        right = generated / name
        if left.read_bytes() != right.read_bytes():
            raise RuntimeError(f"reanalyzed output differs: {left}")


def reanalyze() -> None:
    with tempfile.TemporaryDirectory(prefix="kuasar-pmem-audit-") as temporary:
        output = Path(temporary)
        run_analysis(
            [
                sys.executable,
                str(HARNESS / "analyze_three_path_evidence.py"),
                "--input",
                str(RESULTS / "three-path"),
                "--output",
                str(output / "three-path"),
            ]
        )
        compare_files(
            RESULTS / "three-path" / "analysis",
            output / "three-path",
            (
                "audit.json",
                "analysis.json",
                "cell_summary.csv",
                "paired_comparisons.csv",
                "three_path_performance.md",
            ),
        )

        run_analysis(
            [
                sys.executable,
                str(HARNESS / "analyze_cache_backing.py"),
                "--raw-dir",
                str(RESULTS / "cache-backing" / "raw"),
                "--output-dir",
                str(output / "cache-backing"),
            ]
        )
        compare_files(
            RESULTS / "cache-backing" / "analysis",
            output / "cache-backing",
            (
                "analysis.json",
                "cell_summary.csv",
                "backing_pairs.csv",
                "backing_summary.csv",
                "transport_pairs.csv",
                "transport_summary.csv",
                "sharing_summary.csv",
                "cache_backing_performance.md",
            ),
        )

        run_analysis(
            [
                sys.executable,
                str(HARNESS / "analyze_full_tree_backing.py"),
                "--raw-dir",
                str(RESULTS / "full-tree" / "raw"),
                "--output-dir",
                str(output / "full-tree"),
                "--rounds",
                "10",
            ]
        )
        compare_files(
            RESULTS / "full-tree" / "analysis",
            output / "full-tree",
            (
                "cell_summary.csv",
                "backing_summary.csv",
                "transport_summary.csv",
                "sharing_summary.csv",
                "full_tree_backing_performance.md",
            ),
        )
    print("reanalyzed JSON/CSV/Markdown: byte-for-byte PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reanalyze", action="store_true")
    args = parser.parse_args()

    total = sum(verify_functional(run) for run in FUNCTIONAL_RUNS)
    current_worker = load_worker(HARNESS, "worker_pressure")
    total += sum(
        verify_pressure(name, directory, expected, current_worker)
        for name, directory, expected in PRESSURE_RUNS
    )
    if total != 608:
        raise RuntimeError(f"expected 608 archived outcomes, got {total}")
    print(f"formal archive: {total} outcomes PASS")
    if args.reanalyze:
        reanalyze()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
