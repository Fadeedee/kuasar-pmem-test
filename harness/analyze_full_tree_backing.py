#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path


MODES = ("vhost-user-blk-shared-cache", "lazy-pmem")
BACKINGS = ("file", "memfd")
LABELS = {
    "vhost-user-blk-shared-cache": "Shared BLK",
    "lazy-pmem": "Lazy PMEM",
}


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires samples")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "min": min(values),
        "max": max(values),
    }


def metric(row: dict, name: str) -> float:
    metrics = row["metrics"]
    if name == "application_ready_seconds":
        return float(row["group"]["application_ready_seconds"])
    if name == "tree_scan_seconds":
        return float(row["group"]["first_operation_max_seconds"])
    if name == "measured_memory_bytes":
        return float(metrics["held_memory_current_bytes"]) - float(
            metrics["prelaunch_memory_current_bytes"]
        )
    if name == "total_memory_bytes":
        return float(metrics["held_memory_current_bytes"]) - float(
            metrics["worker_baseline_memory_current_bytes"]
        )
    if name == "operation_cpu_usec":
        return float(metrics["operation_complete_cpu_usage_usec"]) - float(
            metrics["prelaunch_cpu_usage_usec"]
        )
    if name == "host_writes_bytes":
        return float(metrics["held_io_wbytes"]) - float(metrics["prelaunch_io_wbytes"])
    if name == "total_pss_kib":
        return float(row["secondary_pss"]["total_kib"])
    raise ValueError(f"unknown metric: {name}")


METRICS = (
    "application_ready_seconds",
    "tree_scan_seconds",
    "measured_memory_bytes",
    "total_memory_bytes",
    "operation_cpu_usec",
    "host_writes_bytes",
    "total_pss_kib",
)


def load_rows(raw_dir: Path, rounds: int) -> tuple[list[dict], int]:
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(raw_dir.glob("*.json"))
    ]
    if not rows:
        raise ValueError(f"no samples in {raw_dir}")
    vm_counts = sorted({int(row["vm_count"]) for row in rows})
    expected = {
        (round_number, vm_count, mode, backing)
        for round_number in range(1, rounds + 1)
        for vm_count in vm_counts
        for mode in MODES
        for backing in BACKINGS
    }
    actual = {
        (
            int(row["round"]),
            int(row["vm_count"]),
            str(row["mode"]),
            str(row["cache_backing"]),
        )
        for row in rows
    }
    if len(rows) != len(actual) or actual != expected:
        raise ValueError("full-tree sample matrix is incomplete or contains duplicates")

    tree_bytes = set()
    for row in rows:
        workload = row["workload"]
        if workload.get("name") != "full-tree-scan":
            raise ValueError("unexpected workload")
        values = [int(value) for value in workload.get("bytes_per_vm", [])]
        if (
            len(values) != int(row["vm_count"])
            or len(set(values)) != 1
            or values[0] <= 0
        ):
            raise ValueError("full-tree result is incomplete or inconsistent")
        tree_bytes.add(values[0])
        counters = row["counters"]
        if (
            int(counters["accelerator"]["measured"]["read_range_bytes"]) != 0
            or int(counters["lazyd"]["measured"]["materialized_bytes"]) != 0
        ):
            raise ValueError("measured full-tree scan fetched remote data")
        mapping = row.get("pmem_mappings")
        if row["mode"] == "lazy-pmem":
            if not mapping or len(mapping["mapping_identities"]) != 1:
                raise ValueError("PMEM sample does not map one shared cache identity")
        elif mapping is not None:
            raise ValueError("BLK sample unexpectedly reports PMEM mappings")
    if len(tree_bytes) != 1:
        raise ValueError("tree byte count differs across samples")
    return rows, next(iter(tree_bytes))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def cell_summaries(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[int, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(int(row["vm_count"]), row["mode"], row["cache_backing"])].append(row)
    output = []
    for (vm_count, mode, backing), samples in sorted(groups.items()):
        item = {
            "vm_count": vm_count,
            "mode": mode,
            "cache_backing": backing,
            "samples": len(samples),
        }
        for name in METRICS:
            for field, value in summary([metric(row, name) for row in samples]).items():
                item[f"{name}_{field}"] = value
        output.append(item)
    return output


def paired_summaries(rows: list[dict], dimension: str) -> list[dict]:
    if dimension == "cache_backing":
        baseline, variant = "file", "memfd"
        fixed = ("vm_count", "mode")
    elif dimension == "mode":
        baseline, variant = "vhost-user-blk-shared-cache", "lazy-pmem"
        fixed = ("vm_count", "cache_backing")
    else:
        raise ValueError(f"unknown pair dimension: {dimension}")

    pairs: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        key = tuple(row[name] for name in fixed) + (row["round"],)
        pairs[key][row[dimension]] = row
    grouped: dict[tuple, list[dict[str, float]]] = defaultdict(list)
    for key, pair in pairs.items():
        if set(pair) != {baseline, variant}:
            raise ValueError(f"incomplete {dimension} pair: {key}")
        values = {}
        for name in METRICS:
            base = metric(pair[baseline], name)
            candidate = metric(pair[variant], name)
            values[name] = (base - candidate) / base * 100 if base else 0.0
        grouped[key[:-1]].append(values)

    output = []
    for key, samples in sorted(grouped.items()):
        item = {name: key[index] for index, name in enumerate(fixed)}
        item["pairs"] = len(samples)
        for name in METRICS:
            improvements = [sample[name] for sample in samples]
            item[f"{name}_improvement_p50"] = percentile(improvements, 0.50)
            item[f"{name}_improvement_p95"] = percentile(improvements, 0.95)
            item[f"{name}_win_rate"] = (
                sum(value > 0 for value in improvements) / len(improvements)
            )
        output.append(item)
    return output


def sharing_summaries(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["mode"] == "lazy-pmem":
            groups[(int(row["vm_count"]), row["cache_backing"])].append(row)
    output = []
    for (vm_count, backing), samples in sorted(groups.items()):
        rss = [float(row["pmem_mappings"]["mapped_rss_kib"]) for row in samples]
        pss = [float(row["pmem_mappings"]["mapped_pss_kib"]) for row in samples]
        identities = [
            len(row["pmem_mappings"]["mapping_identities"]) for row in samples
        ]
        output.append(
            {
                "vm_count": vm_count,
                "cache_backing": backing,
                "samples": len(samples),
                "mapped_rss_kib_p50": percentile(rss, 0.50),
                "mapped_pss_kib_p50": percentile(pss, 0.50),
                "rss_pss_ratio_p50": percentile(
                    [left / right for left, right in zip(rss, pss)], 0.50
                ),
                "mapping_identities_p50": percentile(identities, 0.50),
            }
        )
    return output


def indexed(rows: list[dict], keys: tuple[str, ...]) -> dict[tuple, dict]:
    return {tuple(row[key] for key in keys): row for row in rows}


def mib(value: float) -> float:
    return value / (1024 * 1024)


def write_report(
    path: Path,
    *,
    rounds: int,
    tree_bytes: int,
    cells: list[dict],
    backing_pairs: list[dict],
    transport_pairs: list[dict],
    sharing: list[dict],
) -> None:
    cell = indexed(cells, ("vm_count", "mode", "cache_backing"))
    backing = indexed(backing_pairs, ("vm_count", "mode"))
    transport = indexed(transport_pairs, ("vm_count", "cache_backing"))
    shared = indexed(sharing, ("vm_count", "cache_backing"))
    vm_counts = sorted({int(row["vm_count"]) for row in cells})
    lines = [
        "# Lazy cache backing: full-tree evidence",
        "",
        (
            f"Each VM reads {tree_bytes / (1024 * 1024):.2f} MiB from the same "
            f"assembled EROFS tree. Every cell contains {rounds} paired rounds and the "
            "plaintext cache is materialized before the measured group."
        ),
        "",
        "## Cell medians",
        "",
        "| VMs | Transport | Backing | App Ready p50/p95 (s) | Tree scan p50/p95 (s) | Steady-state cgroup memory delta p50 (MiB) | Operation CPU p50 (s) |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for vm_count in vm_counts:
        for mode in MODES:
            for cache_backing in BACKINGS:
                row = cell[(vm_count, mode, cache_backing)]
                lines.append(
                    f"| {vm_count} | {LABELS[mode]} | {cache_backing} | "
                    f"{row['application_ready_seconds_p50']:.3f}/"
                    f"{row['application_ready_seconds_p95']:.3f} | "
                    f"{row['tree_scan_seconds_p50']:.3f}/"
                    f"{row['tree_scan_seconds_p95']:.3f} | "
                    f"{mib(row['total_memory_bytes_p50']):.1f} | "
                    f"{row['operation_cpu_usec_p50'] / 1_000_000:.2f} |"
                )

    lines.extend(
        [
            "",
            "## Memfd relative to file",
            "",
            "Positive values mean memfd is lower. Win rate is based on paired rounds.",
            "",
            "| VMs | Transport | Tree scan median / wins | Steady-state cgroup memory delta median / wins | Host writes median / wins |",
            "|---:|---|---:|---:|---:|",
        ]
    )
    for vm_count in vm_counts:
        for mode in MODES:
            row = backing[(vm_count, mode)]
            lines.append(
                f"| {vm_count} | {LABELS[mode]} | "
                f"{row['tree_scan_seconds_improvement_p50']:+.1f}% / "
                f"{row['tree_scan_seconds_win_rate']:.0%} | "
                f"{row['total_memory_bytes_improvement_p50']:+.1f}% / "
                f"{row['total_memory_bytes_win_rate']:.0%} | "
                f"{row['host_writes_bytes_improvement_p50']:+.1f}% / "
                f"{row['host_writes_bytes_win_rate']:.0%} |"
            )

    lines.extend(
        [
            "",
            "## Lazy PMEM relative to shared-cache BLK",
            "",
            "Positive values mean Lazy PMEM is lower under the same backing.",
            "",
            "| VMs | Backing | Tree scan median / wins | Post-launch cgroup memory delta median / wins | Steady-state cgroup memory delta median / wins | CPU median / wins |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    for vm_count in vm_counts:
        for cache_backing in BACKINGS:
            row = transport[(vm_count, cache_backing)]
            lines.append(
                f"| {vm_count} | {cache_backing} | "
                f"{row['tree_scan_seconds_improvement_p50']:+.1f}% / "
                f"{row['tree_scan_seconds_win_rate']:.0%} | "
                f"{row['measured_memory_bytes_improvement_p50']:+.1f}% / "
                f"{row['measured_memory_bytes_win_rate']:.0%} | "
                f"{row['total_memory_bytes_improvement_p50']:+.1f}% / "
                f"{row['total_memory_bytes_win_rate']:.0%} | "
                f"{row['operation_cpu_usec_improvement_p50']:+.1f}% / "
                f"{row['operation_cpu_usec_win_rate']:.0%} |"
            )

    lines.extend(
        [
            "",
            "## PMEM mapped-page sharing",
            "",
            "| VMs | Backing | Mapped RSS/PSS p50 (MiB) | RSS/PSS | Cache identities |",
            "|---:|---|---:|---:|---:|",
        ]
    )
    for vm_count in vm_counts:
        for cache_backing in BACKINGS:
            row = shared[(vm_count, cache_backing)]
            lines.append(
                f"| {vm_count} | {cache_backing} | "
                f"{row['mapped_rss_kib_p50'] / 1024:.1f}/"
                f"{row['mapped_pss_kib_p50'] / 1024:.1f} | "
                f"{row['rss_pss_ratio_p50']:.2f} | "
                f"{row['mapping_identities_p50']:.0f} |"
            )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args()
    rows, tree_bytes = load_rows(args.raw_dir, args.rounds)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    cells = cell_summaries(rows)
    backing = paired_summaries(rows, "cache_backing")
    transport = paired_summaries(rows, "mode")
    sharing = sharing_summaries(rows)
    write_csv(output / "cell_summary.csv", cells)
    write_csv(output / "backing_summary.csv", backing)
    write_csv(output / "transport_summary.csv", transport)
    write_csv(output / "sharing_summary.csv", sharing)
    write_report(
        output / "full_tree_backing_performance.md",
        rounds=args.rounds,
        tree_bytes=tree_bytes,
        cells=cells,
        backing_pairs=backing,
        transport_pairs=transport,
        sharing=sharing,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
