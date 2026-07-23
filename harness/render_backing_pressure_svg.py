#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


WIDTH = 1440
HEIGHT = 650
COLORS = {"file": "#047857", "memfd": "#2563eb"}


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


def load(path: Path) -> dict[tuple[int, str], float]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {
        (int(row["memory_max_mib"]), row["cache_backing"]): float(
            row["pass_rate_percent"]
        )
        for row in rows
    }


def panel(
    data: dict[tuple[int, str], float],
    *,
    title: str,
    rounds: int,
    x0: float,
) -> list[str]:
    limits = sorted({key[0] for key in data})
    left, right, top, bottom = x0 + 75, x0 + 650, 160, 545
    output = [
        text(x0, 130, title, font_size=21, font_weight=600, fill="#111827"),
        line(left, top, left, bottom, stroke="#6b7280"),
        line(left, bottom, right, bottom, stroke="#6b7280"),
    ]
    for tick in range(6):
        value = tick * 20
        y = bottom - value / 100 * (bottom - top)
        output.append(line(left, y, right, y, stroke="#e5e7eb"))
        output.append(
            text(
                left - 10,
                y + 4,
                str(value),
                text_anchor="end",
                font_size=12,
                fill="#4b5563",
            )
        )
    output.append(text(left - 55, top - 12, "% pass", font_size=12, fill="#4b5563"))
    positions = {
        limit: left + index * (right - left) / (len(limits) - 1)
        for index, limit in enumerate(limits)
    }
    for limit, x in positions.items():
        output.append(
            text(
                x,
                bottom + 25,
                str(limit),
                text_anchor="middle",
                font_size=13,
                fill="#374151",
            )
        )
    output.append(
        text(
            (left + right) / 2,
            bottom + 49,
            "MemoryMax (MiB), MemorySwapMax=0",
            text_anchor="middle",
            font_size=13,
            fill="#374151",
        )
    )
    for backing in ("file", "memfd"):
        points = []
        for limit in limits:
            value = data[(limit, backing)]
            x = positions[limit]
            y = bottom - value / 100 * (bottom - top)
            points.append((x, y))
            same_value = data[(limit, "file")] == data[(limit, "memfd")]
            label_y = y + 20 if same_value and backing == "memfd" else y - 11
            output.append(
                text(
                    x,
                    label_y,
                    f"{round(value * rounds / 100)}/{rounds}",
                    text_anchor="middle",
                    font_size=12,
                    fill=COLORS[backing],
                )
            )
        output.append(
            f'<polyline fill="none" stroke="{COLORS[backing]}" stroke-width="3" '
            f'points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}"/>'
        )
        for x, y in points:
            output.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" '
                f'fill="{COLORS[backing]}" stroke="white" stroke-width="1.5"/>'
            )
    return output


def render(
    pmem: dict[tuple[int, str], float],
    blk: dict[tuple[int, str], float],
) -> str:
    output = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="100%" height="100%" fill="white"/>',
        text(48, 46, "8-VM full-tree completion under memory pressure", font_size=28, font_weight=700, fill="#111827"),
        text(
            48,
            74,
            "Same 165.95 MiB EROFS tree; only transport, cache backing, and cgroup limit change",
            font_size=15,
            fill="#4b5563",
        ),
        line(970, 42, 1000, 42, stroke=COLORS["file"], stroke_width=4),
        text(1010, 47, "file", font_size=14, fill="#1f2937"),
        line(1100, 42, 1130, 42, stroke=COLORS["memfd"], stroke_width=4),
        text(1140, 47, "memfd", font_size=14, fill="#1f2937"),
    ]
    output.extend(panel(pmem, title="Lazy PMEM (5 rounds)", rounds=5, x0=48))
    output.extend(panel(blk, title="Shared BLK (3 rounds)", rounds=3, x0=750))
    output.append("</svg>")
    return "\n".join(output) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pmem-summary", type=Path, required=True)
    parser.add_argument("--blk-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        render(load(args.pmem_summary), load(args.blk_summary)), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
