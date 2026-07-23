#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
from pathlib import Path


SERIES = (
    ("vhost-user-blk-shared-cache", "file", "Shared BLK / file", "#4b5563"),
    ("vhost-user-blk-shared-cache", "memfd", "Shared BLK / memfd", "#d97706"),
    ("lazy-pmem", "file", "Lazy PMEM / file", "#047857"),
    ("lazy-pmem", "memfd", "Lazy PMEM / memfd", "#2563eb"),
)
WIDTH = 1440
HEIGHT = 650


def text(x: float, y: float, value: str, **attrs: object) -> str:
    encoded = " ".join(
        f'{name.replace("_", "-")}="{value}"'
        for name, value in {"x": x, "y": y, **attrs}.items()
    )
    return f"<text {encoded}>{html.escape(value)}</text>"


def line(x1: float, y1: float, x2: float, y2: float, **attrs: object) -> str:
    encoded = " ".join(
        f'{name.replace("_", "-")}="{value}"'
        for name, value in {"x1": x1, "y1": y1, "x2": x2, "y2": y2, **attrs}.items()
    )
    return f"<line {encoded}/>"


def load(path: Path) -> dict[tuple[int, str, str], dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return {
            (int(row["vm_count"]), row["mode"], row["cache_backing"]): row
            for row in csv.DictReader(source)
        }


def panel(
    data: dict[tuple[int, str, str], dict[str, str]],
    *,
    metric: str,
    title: str,
    unit: str,
    divisor: float,
    x0: float,
) -> list[str]:
    vm_counts = sorted({key[0] for key in data})
    values = [
        float(data[(vm, mode, backing)][metric]) / divisor
        for vm in vm_counts
        for mode, backing, _, _ in SERIES
    ]
    maximum = max(values) * 1.13
    left, right, top, bottom = x0 + 75, x0 + 650, 160, 545
    output = [
        text(x0, 130, title, font_size=21, font_weight=600, fill="#111827"),
        line(left, top, left, bottom, stroke="#6b7280"),
        line(left, bottom, right, bottom, stroke="#6b7280"),
    ]
    for tick in range(6):
        ratio = tick / 5
        y = bottom - ratio * (bottom - top)
        output.append(line(left, y, right, y, stroke="#e5e7eb"))
        output.append(
            text(
                left - 10,
                y + 4,
                f"{maximum * ratio:.1f}",
                text_anchor="end",
                font_size=12,
                fill="#4b5563",
            )
        )
    output.append(text(left - 55, top - 12, unit, font_size=12, fill="#4b5563"))
    positions = {
        vm: left + index * (right - left) / (len(vm_counts) - 1)
        for index, vm in enumerate(vm_counts)
    }
    for vm, x in positions.items():
        output.append(
            text(x, bottom + 25, str(vm), text_anchor="middle", font_size=13, fill="#374151")
        )
    output.append(
        text(
            (left + right) / 2,
            bottom + 49,
            "Concurrent VMs",
            text_anchor="middle",
            font_size=13,
            fill="#374151",
        )
    )
    for mode, backing, _, color in SERIES:
        points = []
        for vm in vm_counts:
            value = float(data[(vm, mode, backing)][metric]) / divisor
            x = positions[vm]
            y = bottom - value / maximum * (bottom - top)
            points.append((x, y))
        output.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="3" '
            f'points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}"/>'
        )
        for x, y in points:
            output.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" '
                'stroke="white" stroke-width="1.5"/>'
            )
    return output


def render(data: dict[tuple[int, str, str], dict[str, str]]) -> str:
    output = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="100%" height="100%" fill="white"/>',
        text(48, 46, "EROFS full-tree scan: paired medians", font_size=28, font_weight=700, fill="#111827"),
        text(
            48,
            74,
            "Each VM reads 165.95 MiB from the same warm final cache; 10 paired rounds per cell",
            font_size=15,
            fill="#4b5563",
        ),
    ]
    for index, (_, _, label, color) in enumerate(SERIES):
        x = 690 + (index % 2) * 310
        y = 40 + (index // 2) * 28
        output.append(line(x, y, x + 28, y, stroke=color, stroke_width=4))
        output.append(text(x + 38, y + 5, label, font_size=14, fill="#1f2937"))
    output.extend(
        panel(
            data,
            metric="tree_scan_seconds_p50",
            title="Full-tree scan latency",
            unit="seconds",
            divisor=1,
            x0=48,
        )
    )
    output.extend(
        panel(
            data,
            metric="total_memory_bytes_p50",
            title="Steady-state cgroup memory delta",
            unit="MiB",
            divisor=1024 * 1024,
            x0=750,
        )
    )
    output.append("</svg>")
    return "\n".join(output) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(load(args.summary)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
