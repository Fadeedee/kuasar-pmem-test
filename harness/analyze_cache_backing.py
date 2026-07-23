#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable


MODES = ("vhost-user-blk-shared-cache", "lazy-pmem")
BACKINGS = ("file", "memfd")
STATES = ("plaintext-cold", "plaintext-warm")

METRICS = {
    "application_ready_seconds": ("group", "application_ready_seconds"),
    "first_operation_seconds": ("group", "first_operation_max_seconds"),
    "held_memory_bytes": ("metrics", "held_delta_memory_current_bytes"),
    "held_anon_bytes": ("metrics", "held_delta_memory_anon_bytes"),
    "held_file_bytes": ("metrics", "held_delta_memory_file_bytes"),
    "held_shmem_bytes": ("metrics", "held_delta_memory_shmem_bytes"),
    "launch_memory_growth_bytes": ("derived", "launch_memory_growth_bytes"),
    "launch_cpu_usec": ("derived", "launch_cpu_usec"),
    "launch_io_wbytes": ("derived", "launch_io_wbytes"),
    "total_pss_kib": ("secondary_pss", "total_kib"),
}


def percentile(values: Iterable[float], quantile: float) -> float:
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


def summary(values: Iterable[float]) -> dict[str, float | int]:
    samples = list(values)
    return {
        "count": len(samples),
        "mean": statistics.fmean(samples),
        "p50": percentile(samples, 0.50),
        "p95": percentile(samples, 0.95),
        "min": min(samples),
        "max": max(samples),
    }


def value(row: dict[str, object], metric: str) -> float:
    section, field = METRICS[metric]
    metrics = row["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("metrics is not an object")
    if section == "derived":
        if field == "launch_memory_growth_bytes":
            return float(metrics["held_memory_current_bytes"]) - float(
                metrics["prelaunch_memory_current_bytes"]
            )
        if field == "launch_cpu_usec":
            return float(metrics["app_ready_cpu_usage_usec"]) - float(
                metrics["prelaunch_cpu_usage_usec"]
            )
        if field == "launch_io_wbytes":
            return float(metrics["held_io_wbytes"]) - float(
                metrics["prelaunch_io_wbytes"]
            )
        raise ValueError(f"unknown derived metric: {field}")
    section_value = row[section]
    if not isinstance(section_value, dict):
        raise ValueError(f"{section} is not an object")
    return float(section_value[field])


def load_rows(raw_dir: Path) -> list[dict[str, object]]:
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(raw_dir.glob("*.json"))]
    if not rows:
        raise ValueError(f"no raw samples in {raw_dir}")
    hashes = {row["workload"]["response_sha256"] for row in rows}
    if len(hashes) != 1:
        raise ValueError("workload response hashes differ")
    keys = {
        (
            int(row["round"]),
            str(row["source_state"]),
            int(row["vm_count"]),
            str(row["mode"]),
            str(row["cache_backing"]),
        )
        for row in rows
    }
    if len(keys) != len(rows):
        raise ValueError("duplicate benchmark cells")
    return rows


def cell_summaries(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, int, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        groups[
            (
                str(row["source_state"]),
                int(row["vm_count"]),
                str(row["mode"]),
                str(row["cache_backing"]),
            )
        ].append(row)
    output = []
    for (state, vm_count, mode, backing), samples in sorted(groups.items()):
        item: dict[str, object] = {
            "source_state": state,
            "vm_count": vm_count,
            "mode": mode,
            "cache_backing": backing,
            "samples": len(samples),
        }
        for metric in METRICS:
            stats = summary(value(row, metric) for row in samples)
            for name, number in stats.items():
                item[f"{metric}_{name}"] = number
        output.append(item)
    return output


def paired_rows(
    rows: list[dict[str, object]],
    *,
    dimension: str,
    baseline: str,
    variant: str,
) -> list[dict[str, object]]:
    fixed = (
        ("source_state", "vm_count", "mode")
        if dimension == "cache_backing"
        else ("source_state", "vm_count", "cache_backing")
    )
    groups: dict[tuple[object, ...], dict[str, dict[str, object]]] = defaultdict(dict)
    for row in rows:
        key = tuple(row[name] for name in fixed) + (row["round"],)
        groups[key][str(row[dimension])] = row

    output = []
    for key, pair in sorted(groups.items()):
        if set(pair) != {baseline, variant}:
            raise ValueError(f"incomplete {dimension} pair for {key}: {set(pair)}")
        item = {name: key[index] for index, name in enumerate(fixed)}
        item["round"] = key[-1]
        for metric in METRICS:
            base = value(pair[baseline], metric)
            candidate = value(pair[variant], metric)
            item[f"{metric}_baseline"] = base
            item[f"{metric}_variant"] = candidate
            item[f"{metric}_improvement_percent"] = (
                (base - candidate) / base * 100 if base else 0.0
            )
        output.append(item)
    return output


def paired_summaries(
    pairs: list[dict[str, object]], fixed: tuple[str, ...]
) -> list[dict[str, object]]:
    groups: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    for row in pairs:
        groups[tuple(row[name] for name in fixed)].append(row)
    output = []
    for key, samples in sorted(groups.items()):
        item: dict[str, object] = {
            name: key[index] for index, name in enumerate(fixed)
        }
        item["pairs"] = len(samples)
        for metric in METRICS:
            values = [
                float(row[f"{metric}_improvement_percent"]) for row in samples
            ]
            item[f"{metric}_median_improvement_percent"] = statistics.median(values)
            item[f"{metric}_win_rate_percent"] = (
                sum(number > 0 for number in values) / len(values) * 100
            )
        output.append(item)
    return output


def sharing_summaries(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    indexed = {
        (
            int(row["round"]),
            str(row["source_state"]),
            int(row["vm_count"]),
            str(row["mode"]),
            str(row["cache_backing"]),
        ): row
        for row in rows
    }
    groups: dict[tuple[str, int, str], list[dict[str, float]]] = defaultdict(list)
    for row in rows:
        if row["mode"] != "lazy-pmem":
            continue
        state = str(row["source_state"])
        vm_count = int(row["vm_count"])
        backing = str(row["cache_backing"])
        mapping = row["pmem_mappings"]
        if not isinstance(mapping, dict):
            raise ValueError("PMEM sample lacks mapping accounting")
        rss = float(mapping["mapped_rss_kib"])
        pss = float(mapping["mapped_pss_kib"])
        identities = mapping["mapping_identities"]
        if not isinstance(identities, list):
            raise ValueError("mapping identities are not a list")
        blk = indexed[
            (
                int(row["round"]),
                state,
                vm_count,
                "vhost-user-blk-shared-cache",
                backing,
            )
        ]
        vhost = blk["counters"]["vhost_root"]
        groups[(state, vm_count, backing)].append(
            {
                "mapped_rss_kib": rss,
                "mapped_pss_kib": pss,
                "sharing_ratio": rss / pss if pss else 0.0,
                "mapping_identities": float(len(identities)),
                "blk_read_bytes": float(vhost["read_bytes"]),
                "held_memory_saved_bytes": value(blk, "held_memory_bytes")
                - value(row, "held_memory_bytes"),
            }
        )
    output = []
    for (state, vm_count, backing), samples in sorted(groups.items()):
        output.append(
            {
                "source_state": state,
                "vm_count": vm_count,
                "cache_backing": backing,
                "pairs": len(samples),
                **{
                    f"{metric}_p50": statistics.median(
                        sample[metric] for sample in samples
                    )
                    for metric in samples[0]
                },
            }
        )
    return output


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def format_mode(mode: str) -> str:
    return "Shared BLK" if mode == MODES[0] else "Lazy PMEM"


def render_markdown(
    cells: list[dict[str, object]],
    backing: list[dict[str, object]],
    transport: list[dict[str, object]],
    sharing: list[dict[str, object]],
) -> str:
    sample_counts = sorted({int(row["samples"]) for row in cells})
    sample_text = (
        str(sample_counts[0])
        if len(sample_counts) == 1
        else "/".join(str(count) for count in sample_counts)
    )
    lines = [
        "# Lazy cache backing performance",
        "",
        f"All reported samples are retained. Each cell contains {sample_text} paired rounds.",
        "",
        "## Cell medians",
        "",
        "| State | VMs | Transport | Backing | App Ready p50 / p95 (s) | First op p50 (ms) | Held memory p50 (MiB) | CPU to ready p50 (ms) | Host writes p50 (MiB) |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in cells:
        lines.append(
            "| {state} | {vms} | {mode} | {backing} | {ready:.3f} / {ready95:.3f} | "
            "{op:.3f} | {memory:.1f} | {cpu:.1f} | {writes:.1f} |".format(
                state=str(row["source_state"]).replace("plaintext-", ""),
                vms=row["vm_count"],
                mode=format_mode(str(row["mode"])),
                backing=row["cache_backing"],
                ready=float(row["application_ready_seconds_p50"]),
                ready95=float(row["application_ready_seconds_p95"]),
                op=float(row["first_operation_seconds_p50"]) * 1000,
                memory=float(row["held_memory_bytes_p50"]) / 1024 / 1024,
                cpu=float(row["launch_cpu_usec_p50"]) / 1000,
                writes=float(row["launch_io_wbytes_p50"]) / 1024 / 1024,
            )
        )

    lines.extend(
        [
            "",
            "## Memfd relative to file",
            "",
            "Positive values mean memfd is lower; win rate is the fraction of paired rounds in which memfd is lower.",
            "",
            "| State | VMs | Transport | App Ready median / wins | Held memory median / wins | Host writes median / wins |",
            "|---|---:|---|---:|---:|---:|",
        ]
    )
    for row in backing:
        lines.append(
            "| {state} | {vms} | {mode} | {ready:+.1f}% / {ready_wins:.0f}% | "
            "{memory:+.1f}% / {memory_wins:.0f}% | {writes:+.1f}% / {write_wins:.0f}% |".format(
                state=str(row["source_state"]).replace("plaintext-", ""),
                vms=row["vm_count"],
                mode=format_mode(str(row["mode"])),
                ready=float(row["application_ready_seconds_median_improvement_percent"]),
                ready_wins=float(row["application_ready_seconds_win_rate_percent"]),
                memory=float(row["held_memory_bytes_median_improvement_percent"]),
                memory_wins=float(row["held_memory_bytes_win_rate_percent"]),
                writes=float(row["launch_io_wbytes_median_improvement_percent"]),
                write_wins=float(row["launch_io_wbytes_win_rate_percent"]),
            )
        )

    lines.extend(
        [
            "",
            "## Lazy PMEM relative to shared-cache BLK",
            "",
            "Positive values mean Lazy PMEM is lower under the same backing and source state.",
            "",
            "| State | VMs | Backing | App Ready median / wins | First op median / wins | Held memory median / wins | Total PSS median / wins |",
            "|---|---:|---|---:|---:|---:|---:|",
        ]
    )
    for row in transport:
        lines.append(
            "| {state} | {vms} | {backing} | {ready:+.1f}% / {ready_wins:.0f}% | "
            "{op:+.1f}% / {op_wins:.0f}% | {memory:+.1f}% / {memory_wins:.0f}% | "
            "{pss:+.1f}% / {pss_wins:.0f}% |".format(
                state=str(row["source_state"]).replace("plaintext-", ""),
                vms=row["vm_count"],
                backing=row["cache_backing"],
                ready=float(row["application_ready_seconds_median_improvement_percent"]),
                ready_wins=float(row["application_ready_seconds_win_rate_percent"]),
                op=float(row["first_operation_seconds_median_improvement_percent"]),
                op_wins=float(row["first_operation_seconds_win_rate_percent"]),
                memory=float(row["held_memory_bytes_median_improvement_percent"]),
                memory_wins=float(row["held_memory_bytes_win_rate_percent"]),
                pss=float(row["total_pss_kib_median_improvement_percent"]),
                pss_wins=float(row["total_pss_kib_win_rate_percent"]),
            )
        )
    lines.extend(
        [
            "",
            "## PMEM page-sharing evidence",
            "",
            "Mapped RSS counts the same cache pages in every VMM; mapped PSS divides shared pages by the number of mappings. A ratio close to the VM count and one mapping identity indicate cross-VM sharing of one final cache object.",
            "",
            "| State | VMs | Backing | PMEM mapped RSS / PSS p50 (MiB) | RSS/PSS | Cache identities | BLK root reads p50 (MiB) | Held memory saved p50 (MiB) |",
            "|---|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sharing:
        lines.append(
            "| {state} | {vms} | {backing} | {rss:.1f} / {pss:.1f} | {ratio:.2f} | "
            "{identities:.0f} | {reads:.1f} | {saved:+.1f} |".format(
                state=str(row["source_state"]).replace("plaintext-", ""),
                vms=row["vm_count"],
                backing=row["cache_backing"],
                rss=float(row["mapped_rss_kib_p50"]) / 1024,
                pss=float(row["mapped_pss_kib_p50"]) / 1024,
                ratio=float(row["sharing_ratio_p50"]),
                identities=float(row["mapping_identities_p50"]),
                reads=float(row["blk_read_bytes_p50"]) / 1024 / 1024,
                saved=float(row["held_memory_saved_bytes_p50"]) / 1024 / 1024,
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = load_rows(args.raw_dir)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    cells = cell_summaries(rows)
    backing_pairs = paired_rows(
        rows, dimension="cache_backing", baseline="file", variant="memfd"
    )
    backing = paired_summaries(
        backing_pairs, ("source_state", "vm_count", "mode")
    )
    transport_pairs = paired_rows(
        rows,
        dimension="mode",
        baseline="vhost-user-blk-shared-cache",
        variant="lazy-pmem",
    )
    transport = paired_summaries(
        transport_pairs, ("source_state", "vm_count", "cache_backing")
    )
    sharing = sharing_summaries(rows)

    write_csv(output_dir / "cell_summary.csv", cells)
    write_csv(output_dir / "backing_pairs.csv", backing_pairs)
    write_csv(output_dir / "backing_summary.csv", backing)
    write_csv(output_dir / "transport_pairs.csv", transport_pairs)
    write_csv(output_dir / "transport_summary.csv", transport)
    write_csv(output_dir / "sharing_summary.csv", sharing)
    analysis = {
        "sample_count": len(rows),
        "response_sha256": rows[0]["workload"]["response_sha256"],
        "cell_summary": cells,
        "backing_summary": backing,
        "transport_summary": transport,
        "sharing_summary": sharing,
    }
    (output_dir / "analysis.json").write_text(
        json.dumps(analysis, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "cache_backing_performance.md").write_text(
        render_markdown(cells, backing, transport, sharing), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
