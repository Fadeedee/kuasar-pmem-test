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
STATES = ("plaintext-cold", "plaintext-warm")
WIDTH = 1440
HEIGHT = 920


def load(path: Path) -> dict[tuple[str, int, str, str], dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    return {
        (
            row["source_state"],
            int(row["vm_count"]),
            row["mode"],
            row["cache_backing"],
        ): row
        for row in rows
    }


def line(x1: float, y1: float, x2: float, y2: float, **attrs: object) -> str:
    values = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, **attrs}
    encoded = " ".join(f'{name.replace("_", "-")}="{value}"' for name, value in values.items())
    return f"<line {encoded}/>"


def text(x: float, y: float, value: str, **attrs: object) -> str:
    values = {"x": x, "y": y, **attrs}
    encoded = " ".join(f'{name.replace("_", "-")}="{value}"' for name, value in values.items())
    return f"<text {encoded}>{html.escape(value)}</text>"


def chart(
    data: dict[tuple[str, int, str, str], dict[str, str]],
    *,
    state: str,
    metric: str,
    title: str,
    unit: str,
    x0: float,
    y0: float,
    width: float,
    height: float,
    divisor: float = 1.0,
) -> list[str]:
    vm_counts = sorted({key[1] for key in data if key[0] == state})
    values = [
        float(data[(state, vm, mode, backing)][metric]) / divisor
        for vm in vm_counts
        for mode, backing, _, _ in SERIES
    ]
    maximum = max(values) * 1.15
    left, top, right, bottom = x0 + 72, y0 + 42, x0 + width - 24, y0 + height - 58
    output = [
        text(x0, y0 + 18, title, font_size=20, font_weight=600, fill="#111827"),
        line(left, top, left, bottom, stroke="#6b7280", stroke_width=1),
        line(left, bottom, right, bottom, stroke="#6b7280", stroke_width=1),
    ]
    for tick in range(5):
        ratio = tick / 4
        y = bottom - ratio * (bottom - top)
        label = maximum * ratio
        output.append(line(left, y, right, y, stroke="#e5e7eb", stroke_width=1))
        output.append(
            text(
                left - 10,
                y + 4,
                f"{label:.1f}",
                text_anchor="end",
                font_size=12,
                fill="#4b5563",
            )
        )
    output.append(
        text(
            left - 55,
            top - 10,
            unit,
            font_size=12,
            fill="#4b5563",
        )
    )
    positions = {
        vm: left + index * (right - left) / max(1, len(vm_counts) - 1)
        for index, vm in enumerate(vm_counts)
    }
    for vm, x in positions.items():
        output.append(
            text(x, bottom + 24, str(vm), text_anchor="middle", font_size=13, fill="#374151")
        )
    output.append(
        text(
            (left + right) / 2,
            bottom + 46,
            "Concurrent VMs",
            text_anchor="middle",
            font_size=13,
            fill="#374151",
        )
    )
    for mode, backing, label, color in SERIES:
        points = []
        for vm in vm_counts:
            number = float(data[(state, vm, mode, backing)][metric]) / divisor
            x = positions[vm]
            y = bottom - number / maximum * (bottom - top)
            points.append((x, y))
        output.append(
            '<polyline fill="none" stroke="{color}" stroke-width="3" points="{points}"/>'.format(
                color=color,
                points=" ".join(f"{x:.1f},{y:.1f}" for x, y in points),
            )
        )
        for x, y in points:
            output.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" stroke="white" stroke-width="1.5"/>'
            )
    return output


def render(data: dict[tuple[str, int, str, str], dict[str, str]]) -> str:
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="100%" height="100%" fill="white"/>',
        text(48, 48, "Lazy cache backing: paired medians", font_size=28, font_weight=700, fill="#111827"),
        text(
            48,
            76,
            "Same image and workload; only transport and lazyd cache backing change",
            font_size=15,
            fill="#4b5563",
        ),
    ]
    legend_x = 690
    for index, (_, _, label, color) in enumerate(SERIES):
        x = legend_x + (index % 2) * 300
        y = 42 + (index // 2) * 27
        elements.append(line(x, y, x + 28, y, stroke=color, stroke_width=4))
        elements.append(text(x + 38, y + 5, label, font_size=14, fill="#1f2937"))

    panel_width, panel_height = 670, 365
    for row, state in enumerate(STATES):
        state_label = "Cold final cache" if state == "plaintext-cold" else "Warm final cache"
        elements.extend(
            chart(
                data,
                state=state,
                metric="application_ready_seconds_p50",
                title=f"{state_label}: Application Ready",
                unit="seconds",
                x0=48,
                y0=120 + row * 390,
                width=panel_width,
                height=panel_height,
            )
        )
        elements.extend(
            chart(
                data,
                state=state,
                metric="held_memory_bytes_p50",
                title=f"{state_label}: held node memory",
                unit="MiB",
                x0=750,
                y0=120 + row * 390,
                width=panel_width,
                height=panel_height,
                divisor=1024 * 1024,
            )
        )
    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(load(args.summary)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
