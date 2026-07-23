#!/usr/bin/env python3
"""Validate and summarize the paired three-path Lazy PMEM benchmark."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import random
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


MODES = (
    "vhost-user-blk",
    "vhost-user-blk-shared-cache",
    "lazy-pmem",
)
MODE_LABELS = {
    "vhost-user-blk": "Current BLK",
    "vhost-user-blk-shared-cache": "BLK + shared cache",
    "lazy-pmem": "Lazy PMEM",
}
MODE_COLORS = {
    "vhost-user-blk": "#3B6EA8",
    "vhost-user-blk-shared-cache": "#D0872B",
    "lazy-pmem": "#2F855A",
}
STATES = ("plaintext-cold", "plaintext-warm")
STATE_LABELS = {
    "plaintext-cold": "Cold plaintext cache",
    "plaintext-warm": "Warm plaintext cache",
}
VM_COUNTS = (1, 2, 4, 8)
MIB = 1024 * 1024


@dataclass(frozen=True)
class MetricSpec:
    name: str
    unit: str
    extract: Callable[[dict], float]


METRICS = (
    MetricSpec(
        "application_ready_seconds",
        "s",
        lambda item: float(item["group"]["application_ready_seconds"]),
    ),
    MetricSpec(
        "first_operation_ms",
        "ms",
        lambda item: float(item["group"]["first_operation_max_seconds"]) * 1000,
    ),
    MetricSpec(
        "held_memory_mib",
        "MiB",
        lambda item: float(item["metrics"]["held_delta_memory_current_bytes"]) / MIB,
    ),
    MetricSpec(
        "total_pss_mib",
        "MiB",
        lambda item: float(item["secondary_pss"]["total_kib"]) / 1024,
    ),
    MetricSpec(
        "app_ready_cpu_seconds",
        "s",
        lambda item: float(item["metrics"]["app_ready_delta_cpu_usage_usec"])
        / 1_000_000,
    ),
    MetricSpec(
        "app_ready_io_read_mib",
        "MiB",
        lambda item: float(item["metrics"]["app_ready_delta_io_rbytes"]) / MIB,
    ),
    MetricSpec(
        "app_ready_io_write_mib",
        "MiB",
        lambda item: float(item["metrics"]["app_ready_delta_io_wbytes"]) / MIB,
    ),
)

COMPARISONS = (
    (
        "shared_vs_current",
        "vhost-user-blk-shared-cache",
        "vhost-user-blk",
    ),
    ("pmem_vs_current", "lazy-pmem", "vhost-user-blk"),
    (
        "pmem_vs_shared",
        "lazy-pmem",
        "vhost-user-blk-shared-cache",
    ),
)


def quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be in [0, 1]")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def bootstrap_median_ci(
    values: Sequence[float],
    *,
    iterations: int,
    seed: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    if not values:
        raise ValueError("bootstrap requires at least one value")
    if iterations <= 0:
        raise ValueError("bootstrap iterations must be positive")
    generator = random.Random(seed)
    source = tuple(float(value) for value in values)
    medians = []
    for _ in range(iterations):
        medians.append(
            statistics.median(generator.choice(source) for _ in range(len(source)))
        )
    tail = (1 - confidence) / 2
    return quantile(medians, tail), quantile(medians, 1 - tail)


def stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("\0".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def load_samples(raw_dir: Path) -> list[dict]:
    samples = []
    for path in sorted(raw_dir.glob("*.json")):
        with path.open(encoding="utf-8") as source:
            item = json.load(source)
        item["_source_file"] = path.name
        samples.append(item)
    return samples


def measured_counters(sample: dict, component: str) -> dict:
    return sample["counters"].get(component, {}).get("measured", {})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_sample_grid(
    samples: Sequence[dict],
    *,
    rounds: int,
    states: Sequence[str] = STATES,
    vm_counts: Sequence[int] = VM_COUNTS,
    modes: Sequence[str] = MODES,
    materialization_max_bytes: int = MIB,
) -> dict:

    _require(
        materialization_max_bytes > 0,
        "materialization window must be positive",
    )
    expected = {
        (round_number, state, vm_count, mode)
        for round_number in range(1, rounds + 1)
        for state in states
        for vm_count in vm_counts
        for mode in modes
    }
    actual = [
        (
            int(sample["round"]),
            sample["source_state"],
            int(sample["vm_count"]),
            sample["mode"],
        )
        for sample in samples
    ]
    duplicates = sorted(key for key in set(actual) if actual.count(key) != 1)
    missing = sorted(expected - set(actual))
    extra = sorted(set(actual) - expected)
    _require(
        not duplicates and not missing and not extra and len(actual) == len(expected),
        f"sample grid is incomplete: duplicates={duplicates}, missing={missing}, extra={extra}",
    )

    response_hashes = {sample["workload"]["response_sha256"] for sample in samples}
    response_sizes = {int(sample["workload"]["response_bytes"]) for sample in samples}
    _require(len(response_hashes) == 1, "workload response hashes differ")
    _require(len(response_sizes) == 1, "workload response sizes differ")

    cgroups = []
    lazy_modes = tuple(mode for mode in modes if mode != "vhost-user-blk")
    cold_materialized = {mode: set() for mode in lazy_modes}
    cold_materialized_max = {mode: set() for mode in lazy_modes}
    for sample in samples:
        label = (
            f"round={sample['round']} state={sample['source_state']} "
            f"vms={sample['vm_count']} mode={sample['mode']}"
        )
        cgroup = sample["metrics"]["worker_baseline_cgroup_path"]
        _require(
            cgroup.startswith("/sys/fs/cgroup/system.slice/klp-e-"),
            f"{label}: sample did not run in the benchmark service cgroup",
        )
        cgroups.append(cgroup)
        vhost = sample["counters"]["vhost_root"]
        _require(int(vhost["read_errors"]) == 0, f"{label}: vhost read error")

        accelerator = measured_counters(sample, "accelerator")
        lazyd = measured_counters(sample, "lazyd")
        cloud_hypervisor = measured_counters(sample, "cloud_hypervisor")
        if sample["mode"] == "vhost-user-blk":
            _require(not lazyd, f"{label}: current BLK unexpectedly used lazyd")
            _require(
                not cloud_hypervisor,
                f"{label}: current BLK unexpectedly used lazy PMEM counters",
            )
            _require(
                int(accelerator.get("read_range_bytes", 0)) == 0,
                f"{label}: current BLK unexpectedly used the range service",
            )
            continue

        _require(bool(lazyd), f"{label}: missing lazyd counters")
        if sample["source_state"] == "plaintext-cold":
            materialized = int(lazyd["materialized_bytes"])
            remote = int(accelerator["read_range_bytes"])
            materialized_max = int(lazyd["materialized_max_bytes"])
            remote_max = int(accelerator["read_range_max_bytes"])
            _require(materialized > 0, f"{label}: cold cache materialized no data")
            _require(
                materialized == remote,
                f"{label}: accelerator and lazyd materialized bytes differ",
            )
            _require(
                materialized_max == remote_max,
                f"{label}: lazyd and accelerator maximum range bytes differ",
            )
            _require(
                0 < materialized_max <= materialization_max_bytes,
                f"{label}: materialization window exceeded: "
                f"{materialized_max} > {materialization_max_bytes}",
            )
            cold_materialized[sample["mode"]].add(materialized)
            cold_materialized_max[sample["mode"]].add(materialized_max)
        else:
            _require(
                int(lazyd["materialized_bytes"]) == 0
                and int(accelerator["read_range_bytes"]) == 0
                and int(lazyd["materialized_max_bytes"]) == 0
                and int(accelerator["read_range_max_bytes"]) == 0,
                f"{label}: warm measured phase fetched remote data",
            )

        if sample["mode"] == "vhost-user-blk-shared-cache":
            _require(
                int(lazyd["fetch_requests"]) == int(vhost["read_requests"]),
                f"{label}: shared BLK requests did not match lazyd FETCH requests",
            )
            _require(
                not cloud_hypervisor,
                f"{label}: shared BLK unexpectedly emitted PMEM counters",
            )
            continue

        required = (
            "data_faults",
            "padding_faults",
            "fetch_requests",
            "fetch_request_bytes",
            "mmap_ranges",
            "mmap_bytes",
            "wakes",
        )
        _require(
            all(name in cloud_hypervisor for name in required),
            f"{label}: incomplete Cloud Hypervisor counters",
        )
        _require(
            int(cloud_hypervisor["data_faults"])
            == int(cloud_hypervisor["fetch_requests"])
            == int(cloud_hypervisor["mmap_ranges"])
            == int(cloud_hypervisor["wakes"]),
            f"{label}: fault/FETCH/mmap/wake counters differ",
        )
        _require(
            int(cloud_hypervisor["fetch_requests"]) == int(lazyd["fetch_requests"])
            and int(cloud_hypervisor["fetch_request_bytes"])
            == int(lazyd["fetch_request_bytes"])
            and int(cloud_hypervisor["mmap_bytes"])
            == int(lazyd["fetch_returned_range_bytes"]),
            f"{label}: Cloud Hypervisor and lazyd counters differ",
        )
        mappings = sample["pmem_mappings"]
        _require(mappings is not None, f"{label}: missing PMEM mapping summary")
        _require(
            len(mappings["mapping_identities"]) == 1,
            f"{label}: VMs did not map one shared cache identity",
        )
        _require(
            int(mappings["mapped_private_dirty_kib"]) == 0,
            f"{label}: lazy root mapping contains private dirty pages",
        )
        _require(
            int(mappings["mapped_pss_kib"]) <= int(mappings["mapped_rss_kib"]),
            f"{label}: mapped PSS exceeds mapped RSS",
        )
        if int(sample["vm_count"]) > 1:
            _require(
                int(mappings["mapped_shared_clean_kib"]) > 0,
                f"{label}: multi-VM PMEM mappings are not shared clean",
            )

    _require(len(cgroups) == len(set(cgroups)), "benchmark cgroups are not unique")
    for mode in lazy_modes:
        _require(
            len(cold_materialized[mode]) == 1,
            f"{mode} cold materialized working set changed across samples: "
            f"{cold_materialized[mode]}",
        )
        _require(
            len(cold_materialized_max[mode]) == 1,
            f"{mode} cold maximum materialization changed across samples: "
            f"{cold_materialized_max[mode]}",
        )
    materialized_by_mode = {
        mode: next(iter(cold_materialized[mode])) for mode in lazy_modes
    }
    materialized_max_by_mode = {
        mode: next(iter(cold_materialized_max[mode])) for mode in lazy_modes
    }
    return {
        "sample_count": len(samples),
        "cell_count": len(states) * len(vm_counts) * len(modes),
        "rounds_per_cell": rounds,
        "response_sha256": next(iter(response_hashes)),
        "response_bytes": next(iter(response_sizes)),
        "cold_materialized_bytes_by_mode": materialized_by_mode,
        "cold_materialized_max_bytes_by_mode": materialized_max_by_mode,
        "materialization_max_bytes": materialization_max_bytes,
        "unique_cgroups": len(set(cgroups)),
        "invariants": {
            "complete_paired_grid": True,
            "workload_output_identical": True,
            "vhost_read_errors_zero": True,
            "warm_remote_fetch_bytes_zero": True,
            "materialization_window_bounded": True,
            "transport_working_sets_accounted_separately": True,
            "pmem_fault_fetch_mmap_wake_balanced": True,
            "pmem_single_cache_identity": True,
            "pmem_private_dirty_zero": True,
        },
    }


def group_samples(samples: Sequence[dict]) -> dict[tuple[str, int, str], list[dict]]:
    grouped: dict[tuple[str, int, str], list[dict]] = {}
    for sample in samples:
        key = (sample["source_state"], int(sample["vm_count"]), sample["mode"])
        grouped.setdefault(key, []).append(sample)
    for group in grouped.values():
        group.sort(key=lambda sample: int(sample["round"]))
    return grouped


def build_cell_summaries(
    samples: Sequence[dict], metrics: Sequence[MetricSpec] = METRICS
) -> list[dict]:
    rows = []
    for (state, vm_count, mode), group in sorted(group_samples(samples).items()):
        row = {
            "source_state": state,
            "vm_count": vm_count,
            "mode": mode,
            "samples": len(group),
        }
        for metric in metrics:
            values = [metric.extract(sample) for sample in group]
            row[f"{metric.name}_median"] = statistics.median(values)
            row[f"{metric.name}_p95"] = quantile(values, 0.95)
            row[f"{metric.name}_min"] = min(values)
            row[f"{metric.name}_max"] = max(values)
        row["accelerator_read_range_bytes_median"] = statistics.median(
            int(measured_counters(sample, "accelerator").get("read_range_bytes", 0))
            for sample in group
        )
        row["accelerator_read_range_max_bytes_median"] = statistics.median(
            int(measured_counters(sample, "accelerator").get("read_range_max_bytes", 0))
            for sample in group
        )
        row["lazyd_materialized_bytes_median"] = statistics.median(
            int(measured_counters(sample, "lazyd").get("materialized_bytes", 0))
            for sample in group
        )
        row["lazyd_materialized_max_bytes_median"] = statistics.median(
            int(measured_counters(sample, "lazyd").get("materialized_max_bytes", 0))
            for sample in group
        )
        row["lazyd_fetch_requests_median"] = statistics.median(
            int(measured_counters(sample, "lazyd").get("fetch_requests", 0))
            for sample in group
        )
        row["vhost_read_requests_median"] = statistics.median(
            int(sample["counters"]["vhost_root"]["read_requests"])
            for sample in group
        )
        if mode == "lazy-pmem":
            for name in (
                "mapped_rss_kib",
                "mapped_pss_kib",
                "mapped_shared_clean_kib",
            ):
                row[f"pmem_{name}_median"] = statistics.median(
                    int(sample["pmem_mappings"][name]) for sample in group
                )
                row[f"pmem_{name}_p95"] = quantile(
                    [int(sample["pmem_mappings"][name]) for sample in group], 0.95
                )
        rows.append(row)
    return rows


def build_paired_comparisons(
    samples: Sequence[dict],
    *,
    metrics: Sequence[MetricSpec] = METRICS,
    bootstrap_iterations: int = 20_000,
) -> list[dict]:
    indexed = {
        (
            sample["source_state"],
            int(sample["vm_count"]),
            int(sample["round"]),
            sample["mode"],
        ): sample
        for sample in samples
    }
    states = sorted({sample["source_state"] for sample in samples})
    vm_counts = sorted({int(sample["vm_count"]) for sample in samples})
    rounds = sorted({int(sample["round"]) for sample in samples})
    rows = []
    for state in states:
        for vm_count in vm_counts:
            for comparison, left_mode, baseline_mode in COMPARISONS:
                for metric in metrics:
                    left_values = []
                    baseline_values = []
                    deltas = []
                    improvements = []
                    for round_number in rounds:
                        left = metric.extract(
                            indexed[(state, vm_count, round_number, left_mode)]
                        )
                        baseline = metric.extract(
                            indexed[(state, vm_count, round_number, baseline_mode)]
                        )
                        left_values.append(left)
                        baseline_values.append(baseline)
                        deltas.append(left - baseline)
                        if baseline != 0:
                            improvements.append((baseline - left) / baseline * 100)
                    seed = stable_seed(state, vm_count, comparison, metric.name)
                    delta_ci = bootstrap_median_ci(
                        deltas, iterations=bootstrap_iterations, seed=seed
                    )
                    improvement_ci = (
                        bootstrap_median_ci(
                            improvements,
                            iterations=bootstrap_iterations,
                            seed=seed ^ 0xA5A5A5A5,
                        )
                        if improvements
                        else (None, None)
                    )
                    rows.append(
                        {
                            "source_state": state,
                            "vm_count": vm_count,
                            "comparison": comparison,
                            "left_mode": left_mode,
                            "baseline_mode": baseline_mode,
                            "metric": metric.name,
                            "unit": metric.unit,
                            "rounds": len(rounds),
                            "left_median": statistics.median(left_values),
                            "left_p95": quantile(left_values, 0.95),
                            "baseline_median": statistics.median(baseline_values),
                            "baseline_p95": quantile(baseline_values, 0.95),
                            "paired_delta_median": statistics.median(deltas),
                            "paired_delta_ci_low": delta_ci[0],
                            "paired_delta_ci_high": delta_ci[1],
                            "improvement_pct_median": (
                                statistics.median(improvements) if improvements else None
                            ),
                            "improvement_pct_ci_low": improvement_ci[0],
                            "improvement_pct_ci_high": improvement_ci[1],
                            "left_better_rounds": sum(delta < 0 for delta in deltas),
                            "equal_rounds": sum(delta == 0 for delta in deltas),
                        }
                    )
    return rows


def write_csv(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        raise ValueError(f"cannot write empty CSV {path}")
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summary_index(rows: Sequence[dict]) -> dict[tuple[str, int, str], dict]:
    return {
        (row["source_state"], int(row["vm_count"]), row["mode"]): row
        for row in rows
    }


def axis_positions(values: Sequence[int], start: float, width: float) -> dict[int, float]:
    if not values:
        raise ValueError("axis requires at least one value")
    if len(values) == 1:
        return {int(values[0]): start + width / 2}
    return {
        int(value): start + index * width / (len(values) - 1)
        for index, value in enumerate(values)
    }


def svg_line_chart(
    rows: Sequence[dict],
    *,
    metric: str,
    title: str,
    y_label: str,
    output: Path,
    states: Sequence[str] = STATES,
    vm_counts: Sequence[int] = VM_COUNTS,
    modes: Sequence[str] = MODES,
) -> None:
    if not states or not vm_counts or not modes:
        raise ValueError("chart grid dimensions must not be empty")
    index = summary_index(rows)
    width, height = 1200, 540
    margin_x, panel_gap = 70, 80
    panel_width = (width - 2 * margin_x - panel_gap * (len(states) - 1)) / len(states)
    plot_top, plot_height = 95, 350
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2}" y="34" text-anchor="middle" font-family="sans-serif" font-size="22" font-weight="600">{html.escape(title)}</text>',
    ]
    all_values = [
        float(row[f"{metric}_{suffix}"])
        for row in rows
        for suffix in ("median", "p95")
    ]
    y_max = max(all_values) * 1.12
    if y_max == 0:
        y_max = 1
    for panel, state in enumerate(states):
        left = margin_x + panel * (panel_width + panel_gap)
        bottom = plot_top + plot_height
        elements.append(
            f'<text x="{left + panel_width / 2}" y="70" text-anchor="middle" font-family="sans-serif" font-size="16" font-weight="600">{STATE_LABELS[state]}</text>'
        )
        for tick in range(6):
            value = y_max * tick / 5
            y = bottom - plot_height * tick / 5
            elements.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{left + panel_width}" y2="{y:.1f}" stroke="#d7dde5" stroke-width="1"/>'
            )
            elements.append(
                f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11" fill="#45505d">{value:.1f}</text>'
            )
        elements.append(
            f'<line x1="{left}" y1="{plot_top}" x2="{left}" y2="{bottom}" stroke="#303842" stroke-width="1.2"/>'
        )
        elements.append(
            f'<line x1="{left}" y1="{bottom}" x2="{left + panel_width}" y2="{bottom}" stroke="#303842" stroke-width="1.2"/>'
        )
        x_positions = axis_positions(vm_counts, left, panel_width)
        for count, x in x_positions.items():
            elements.append(
                f'<text x="{x:.1f}" y="{bottom + 22}" text-anchor="middle" font-family="sans-serif" font-size="12">{count}</text>'
            )
        for mode in modes:
            points = []
            for count in vm_counts:
                row = index[(state, count, mode)]
                median_value = float(row[f"{metric}_median"])
                p95_value = float(row[f"{metric}_p95"])
                x = x_positions[count]
                y = bottom - median_value / y_max * plot_height
                y95 = bottom - p95_value / y_max * plot_height
                points.append((x, y))
                elements.append(
                    f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{y95:.1f}" stroke="{MODE_COLORS[mode]}" stroke-width="1.4"/>'
                )
                elements.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{MODE_COLORS[mode]}"/>'
                )
            path = " ".join(
                ("M" if index_value == 0 else "L") + f" {x:.1f} {y:.1f}"
                for index_value, (x, y) in enumerate(points)
            )
            elements.append(
                f'<path d="{path}" fill="none" stroke="{MODE_COLORS[mode]}" stroke-width="2.4"/>'
            )
        elements.append(
            f'<text x="{left + panel_width / 2}" y="{bottom + 48}" text-anchor="middle" font-family="sans-serif" font-size="13">Concurrent VMs</text>'
        )
    elements.append(
        f'<text x="18" y="{plot_top + plot_height / 2}" text-anchor="middle" transform="rotate(-90 18 {plot_top + plot_height / 2})" font-family="sans-serif" font-size="13">{html.escape(y_label)}</text>'
    )
    legend_x, legend_y = 365, 510
    for index_value, mode in enumerate(modes):
        x = legend_x + index_value * 200
        elements.append(
            f'<line x1="{x}" y1="{legend_y}" x2="{x + 24}" y2="{legend_y}" stroke="{MODE_COLORS[mode]}" stroke-width="3"/>'
        )
        elements.append(
            f'<text x="{x + 31}" y="{legend_y + 4}" font-family="sans-serif" font-size="12">{MODE_LABELS[mode]}</text>'
        )
    elements.append("</svg>")
    output.write_text("\n".join(elements) + "\n", encoding="utf-8")


def svg_pmem_sharing_chart(
    rows: Sequence[dict],
    output: Path,
    *,
    vm_counts: Sequence[int] = VM_COUNTS,
    state: str = "plaintext-warm",
) -> None:
    if not vm_counts:
        raise ValueError("sharing chart requires at least one VM count")
    index = summary_index(rows)
    width, height = 760, 500
    left, top, plot_width, plot_height = 85, 70, 610, 330
    bottom = top + plot_height
    series = (
        ("Mapped RSS", "pmem_mapped_rss_kib_median", "#3B6EA8"),
        ("Mapped PSS", "pmem_mapped_pss_kib_median", "#2F855A"),
    )
    values = [
        float(index[(state, count, "lazy-pmem")][field]) / 1024
        for count in vm_counts
        for _, field, _ in series
    ]
    y_max = max(values) * 1.12
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="380" y="32" text-anchor="middle" font-family="sans-serif" font-size="22" font-weight="600">Lazy PMEM file-page sharing</text>',
    ]
    for tick in range(6):
        value = y_max * tick / 5
        y = bottom - plot_height * tick / 5
        elements.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#d7dde5"/>'
        )
        elements.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11">{value:.1f}</text>'
        )
    x_positions = axis_positions(vm_counts, left, plot_width)
    for count, x in x_positions.items():
        elements.append(
            f'<text x="{x:.1f}" y="{bottom + 22}" text-anchor="middle" font-family="sans-serif" font-size="12">{count}</text>'
        )
    for label, field, color in series:
        points = []
        for count in vm_counts:
            value = float(index[(state, count, "lazy-pmem")][field]) / 1024
            x = x_positions[count]
            y = bottom - value / y_max * plot_height
            points.append((x, y))
            elements.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')
        path = " ".join(
            ("M" if index_value == 0 else "L") + f" {x:.1f} {y:.1f}"
            for index_value, (x, y) in enumerate(points)
        )
        elements.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>'
        )
    elements.extend(
        [
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#303842" stroke-width="1.2"/>',
            f'<line x1="{left}" y1="{bottom}" x2="{left + plot_width}" y2="{bottom}" stroke="#303842" stroke-width="1.2"/>',
            f'<text x="{left + plot_width / 2}" y="{bottom + 48}" text-anchor="middle" font-family="sans-serif" font-size="13">Concurrent VMs</text>',
            f'<text x="20" y="{top + plot_height / 2}" text-anchor="middle" transform="rotate(-90 20 {top + plot_height / 2})" font-family="sans-serif" font-size="13">Mapped memory (MiB)</text>',
        ]
    )
    for index_value, (label, _, color) in enumerate(series):
        x = 245 + index_value * 190
        elements.append(
            f'<line x1="{x}" y1="465" x2="{x + 24}" y2="465" stroke="{color}" stroke-width="3"/>'
        )
        elements.append(
            f'<text x="{x + 31}" y="469" font-family="sans-serif" font-size="12">{label}</text>'
        )
    elements.append("</svg>")
    output.write_text("\n".join(elements) + "\n", encoding="utf-8")


def convert_svg_to_png(svg: Path) -> Path | None:
    converter = shutil.which("rsvg-convert")
    if converter is None:
        return None
    png = svg.with_suffix(".png")
    subprocess.run(
        [converter, "--width", "1800", "--output", str(png), str(svg)],
        check=True,
    )
    return png


def comparison_lookup(rows: Sequence[dict]) -> dict[tuple[str, int, str, str], dict]:
    return {
        (
            row["source_state"],
            int(row["vm_count"]),
            row["comparison"],
            row["metric"],
        ): row
        for row in rows
    }


def format_number(value: float, unit: str) -> str:
    precision = 3 if unit in ("s", "ms") else 1
    return f"{value:.{precision}f}"


def format_improvement(value: float) -> str:
    if value >= 0:
        return f"降低 **{value:.1f}%**"
    return f"增加 **{-value:.1f}%**"


def build_report(
    audit: dict,
    summaries: Sequence[dict],
    comparisons: Sequence[dict],
    *,
    states: Sequence[str] = STATES,
    vm_counts: Sequence[int] = VM_COUNTS,
    modes: Sequence[str] = MODES,
) -> str:
    if not states or not vm_counts or not modes:
        raise ValueError("report grid dimensions must not be empty")
    summary = summary_index(summaries)
    paired = comparison_lookup(comparisons)
    max_vm_count = max(vm_counts)
    rounds = int(audit["rounds_per_cell"])
    lines = [
        "# Kuasar Lazy PMEM three-path performance",
        "",
        "## 方法",
        "",
        "- 对比路径为当前 `manifest:// + vhost-user-blk`、复用同一 lazyd plaintext cache 的 `vhost-user-blk`，以及复用该 cache 的 Lazy PMEM。前两者用于拆分公共 cache 收益，后两者用于拆分 PMEM/DAX transport 增量。",
        "- `plaintext-cold` cell 不启动 warmup VM，shared-cache BLK 与 Lazy PMEM 均从新的空 lazyd cache 开始；测试不写全局 `drop_caches`，因此不宣称宿主机块页绝对冷。",
        "- `plaintext-warm` cell 先用相同模式运行一个 warmup VM，随后在同一组 backend 服务中采集正式 VM；计数器基线在 warmup 后重置。当前 BLK 没有 plaintext cache，只保留其现有 store/host cache 行为。",
        "- 每个样本运行在独立 systemd service cgroup，采集其完整进程树的 memory、CPU 和 I/O；同一 cell 内三种模式轮换执行顺序。工作负载为 nginx 首次请求。",
        "",
        "## 结论",
        "",
    ]
    for state in states:
        ready = paired[
            (state, max_vm_count, "pmem_vs_current", "application_ready_seconds")
        ]
        memory = paired[(state, max_vm_count, "pmem_vs_current", "held_memory_mib")]
        incremental_ready = paired[
            (state, max_vm_count, "pmem_vs_shared", "application_ready_seconds")
        ]
        incremental_memory = paired[
            (state, max_vm_count, "pmem_vs_shared", "held_memory_mib")
        ]
        lines.append(
            f"- **{STATE_LABELS[state]} / {max_vm_count} VM**：Lazy PMEM 相比当前 BLK 的 Application Ready 中位数"
            f"{format_improvement(ready['improvement_pct_median'])}（95% bootstrap CI "
            f"{ready['improvement_pct_ci_low']:.1f}%..{ready['improvement_pct_ci_high']:.1f}%），held cgroup memory "
            f"{format_improvement(memory['improvement_pct_median'])}（CI "
            f"{memory['improvement_pct_ci_low']:.1f}%..{memory['improvement_pct_ci_high']:.1f}%）。"
        )
        lines.append(
            f"- **{STATE_LABELS[state]} / {max_vm_count} VM 的 PMEM 增量**：相比同样复用明文 cache 的 BLK，Application Ready 中位数"
            f"{format_improvement(incremental_ready['improvement_pct_median'])}（CI "
            f"{incremental_ready['improvement_pct_ci_low']:.1f}%..{incremental_ready['improvement_pct_ci_high']:.1f}%，"
            f"{incremental_ready['left_better_rounds']}/{rounds} 轮更快），memory "
            f"{format_improvement(incremental_memory['improvement_pct_median'])}（CI "
            f"{incremental_memory['improvement_pct_ci_low']:.1f}%..{incremental_memory['improvement_pct_ci_high']:.1f}%，"
            f"{incremental_memory['left_better_rounds']}/{rounds} 轮更低）。"
        )
    sharing_state = "plaintext-warm" if "plaintext-warm" in states else states[-1]
    warm_pmem = summary[(sharing_state, max_vm_count, "lazy-pmem")]
    if max_vm_count > 1:
        mapping_summary = (
            f"- {max_vm_count} VM Lazy PMEM 的映射 RSS 中位数为 "
            f"**{warm_pmem['pmem_mapped_rss_kib_median'] / 1024:.1f} MiB**，但 PSS 仅 "
            f"**{warm_pmem['pmem_mapped_pss_kib_median'] / 1024:.1f} MiB**；{rounds} 轮均只有一个 "
            "cache identity，且 private dirty 为 0。"
        )
        sharing_explanation = (
            "映射 RSS 随 VM 数增长表示每个进程都映射并访问了这些页；PSS 按共享者分摊，"
            "用于判断是否复用了相同物理文件页。"
        )
    else:
        mapping_summary = (
            "- 单 VM 预检确认 lazy root 使用唯一 cache identity 且 private dirty 为 0；"
            "跨 VM 文件页共享必须由多 VM 矩阵验证。"
        )
        sharing_explanation = "单 VM 数据不用于证明跨 VM 文件页共享。"
    lines.extend(
        [
            mapping_summary,
            f"- cold 阶段 shared-cache BLK 物化 **{audit['cold_materialized_bytes_by_mode']['vhost-user-blk-shared-cache'] / MIB:.2f} MiB**，Lazy PMEM 物化 **{audit['cold_materialized_bytes_by_mode']['lazy-pmem'] / MIB:.2f} MiB**；两条路径单次 accelerator read-range / lazyd materialization 最大分别为 **{audit['cold_materialized_max_bytes_by_mode']['vhost-user-blk-shared-cache'] / 1024:.0f} KiB** 和 **{audit['cold_materialized_max_bytes_by_mode']['lazy-pmem'] / 1024:.0f} KiB**，均不超过配置窗口 **{audit['materialization_max_bytes'] / 1024:.0f} KiB**。transport 访问模式不同，因此累计工作集分别统计；warm 正式阶段的远端取数为 0。",
            "- Current BLK 与 shared-cache BLK 的差值用于衡量公共明文 cache 的收益；shared-cache BLK 与 Lazy PMEM 的差值用于隔离 PMEM/DAX transport 的增量，不能把前者的收益归因于 PMEM。",
            "- 结果支持的目标场景是同节点、同 trust domain、同镜像的多 VM 复用；它不能单独证明 Lazy PMEM 对所有镜像和单 VM 工作负载都优于 BLK。",
            "",
            "## Application Ready",
            "",
            f"Application Ready 是同组最后一个 VM 输出应用就绪标记的时间，单位秒。表内为 {rounds} 轮中位数 / p95。",
            "",
            "| cache | VMs | Current BLK | BLK + shared cache | Lazy PMEM |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for state in states:
        for vm_count in vm_counts:
            values = []
            for mode in modes:
                row = summary[(state, vm_count, mode)]
                values.append(
                    f"{row['application_ready_seconds_median']:.3f} / {row['application_ready_seconds_p95']:.3f}"
                )
            lines.append(
                f"| {STATE_LABELS[state]} | {vm_count} | " + " | ".join(values) + " |"
            )
    lines.extend(
        [
            "",
            "![Application Ready](application_ready.png)",
            "",
            "## Node memory",
            "",
            "held memory 是独立 benchmark service cgroup 在所有 VM 就绪并完成首次请求后的 current-memory 增量，单位 MiB。",
            "",
            "| cache | VMs | Current BLK | BLK + shared cache | Lazy PMEM |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for state in states:
        for vm_count in vm_counts:
            values = []
            for mode in modes:
                row = summary[(state, vm_count, mode)]
                values.append(
                    f"{row['held_memory_mib_median']:.1f} / {row['held_memory_mib_p95']:.1f}"
                )
            lines.append(
                f"| {STATE_LABELS[state]} | {vm_count} | " + " | ".join(values) + " |"
            )
    lines.extend(
        [
            "",
            "![Held memory](held_memory.png)",
            "",
            "## PMEM page sharing",
            "",
            "![PMEM page sharing](pmem_page_sharing.png)",
            "",
            sharing_explanation,
            "",
            "## CPU",
            "",
            "![CPU to Application Ready](app_ready_cpu.png)",
            "",
            "## 完整性",
            "",
            f"- 样本：{audit['sample_count']}，{audit['cell_count']} 个 cell，每个 cell {audit['rounds_per_cell']} 轮。",
            f"- 工作负载响应：{audit['response_bytes']} bytes，SHA-256 `{audit['response_sha256']}`，{audit['sample_count']} 份完全一致。",
            f"- 独立 transient service cgroup：{audit['unique_cgroups']}。",
            "- vhost read error 为 0；PMEM data-fault/FETCH/mmap/wake 计数逐样本一致。",
            "- `cell_summary.csv` 保存每个 cell 的 median/p95/min/max；`paired_comparisons.csv` 保存按 round 配对的差值、改善百分比和 95% bootstrap CI。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-iterations", type=int, default=20_000)
    args = parser.parse_args()

    manifest_path = args.input / "run-manifest.json"
    with manifest_path.open(encoding="utf-8") as source:
        manifest = json.load(source)["contract"]
    states = tuple(manifest["source_states"])
    vm_counts = tuple(int(value) for value in manifest["vm_counts"])
    modes = tuple(manifest["modes"])
    samples = load_samples(args.input / "raw")
    audit = validate_sample_grid(
        samples,
        rounds=int(manifest["rounds"]),
        states=states,
        vm_counts=vm_counts,
        modes=modes,
        materialization_max_bytes=int(manifest["materialization_max_bytes"]),
    )
    summaries = build_cell_summaries(samples)
    comparisons = build_paired_comparisons(
        samples,
        bootstrap_iterations=args.bootstrap_iterations,
    )

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "audit.json").write_text(
        json.dumps(audit, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(args.output / "cell_summary.csv", summaries)
    write_csv(args.output / "paired_comparisons.csv", comparisons)
    (args.output / "analysis.json").write_text(
        json.dumps(
            {"audit": audit, "cell_summaries": summaries, "paired": comparisons},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    charts = (
        (
            "application_ready.svg",
            "application_ready_seconds",
            "Application Ready by concurrent VM count",
            "Seconds (median with p95 whisker)",
        ),
        (
            "held_memory.svg",
            "held_memory_mib",
            "Whole benchmark cgroup memory",
            "MiB (median with p95 whisker)",
        ),
        (
            "app_ready_cpu.svg",
            "app_ready_cpu_seconds",
            "CPU consumed through Application Ready",
            "CPU seconds (median with p95 whisker)",
        ),
    )
    for filename, metric, title, y_label in charts:
        svg = args.output / filename
        svg_line_chart(
            summaries,
            metric=metric,
            title=title,
            y_label=y_label,
            output=svg,
            states=states,
            vm_counts=vm_counts,
            modes=modes,
        )
        convert_svg_to_png(svg)
    sharing = args.output / "pmem_page_sharing.svg"
    sharing_state = "plaintext-warm" if "plaintext-warm" in states else states[-1]
    svg_pmem_sharing_chart(
        summaries,
        sharing,
        vm_counts=vm_counts,
        state=sharing_state,
    )
    convert_svg_to_png(sharing)

    (args.output / "three_path_performance.md").write_text(
        build_report(
            audit,
            summaries,
            comparisons,
            states=states,
            vm_counts=vm_counts,
            modes=modes,
        ),
        encoding="utf-8",
    )
    print(json.dumps(audit, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
