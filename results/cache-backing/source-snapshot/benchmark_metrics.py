#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path


MEMORY_STAT_FIELDS = (
    "anon",
    "file",
    "kernel",
    "pagetables",
    "slab",
    "shmem",
)
CPU_STAT_FIELDS = ("usage_usec", "user_usec", "system_usec")
IO_STAT_FIELDS = ("rbytes", "wbytes", "rios", "wios")


def parse_counter_summaries(contents: str, marker: str) -> list[dict[str, int]]:
    if not marker:
        raise ValueError("counter marker must not be empty")
    pattern = re.compile(re.escape(marker) + r"=(\{[^\n]*\})")
    summaries: list[dict[str, int]] = []
    for match in pattern.finditer(contents):
        decoded = json.loads(match.group(1))
        if not isinstance(decoded, dict) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in decoded.values()
        ):
            raise ValueError("counter summaries must contain non-negative integers")
        summaries.append({str(name): value for name, value in decoded.items()})
    return summaries


def subtract_counters(final: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    if set(final) != set(baseline):
        raise ValueError("counter snapshots use different fields")
    result: dict[str, int] = {}
    for name in final:
        if final[name] < baseline[name]:
            raise ValueError(f"counter {name} decreased")
        result[name] = final[name] - baseline[name]
    return result


def sum_counters(snapshots: list[dict[str, int]]) -> dict[str, int]:
    if not snapshots:
        return {}
    fields = set(snapshots[0])
    if any(set(snapshot) != fields for snapshot in snapshots[1:]):
        raise ValueError("counter snapshots use different fields")
    return {name: sum(snapshot[name] for snapshot in snapshots) for name in fields}


def cgroup_path_from_proc(contents: str, mount: Path = Path("/sys/fs/cgroup")) -> Path:
    unified = []
    for line in contents.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            unified.append(fields[2])
    if len(unified) != 1:
        raise RuntimeError("process is not in exactly one unified cgroup-v2 hierarchy")

    mount = mount.resolve()
    candidate = (mount / unified[0].lstrip("/")).resolve()
    if not candidate.is_relative_to(mount):
        raise RuntimeError("process cgroup path is outside cgroup mount")
    return candidate


def current_cgroup_path(
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    mount: Path = Path("/sys/fs/cgroup"),
) -> Path:
    return cgroup_path_from_proc(proc_cgroup.read_text(encoding="ascii"), mount)


def parse_memory_stat(contents: str) -> dict[str, int]:
    parsed = {name: 0 for name in MEMORY_STAT_FIELDS}
    for line in contents.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise ValueError(f"invalid memory.stat line: {line!r}")
        name, raw_value = fields
        if name not in parsed:
            continue
        value = int(raw_value)
        if value < 0:
            raise ValueError(f"negative memory.stat value for {name}")
        parsed[name] = value
    return parsed


def parse_cpu_stat(contents: str) -> dict[str, int]:
    parsed = {name: 0 for name in CPU_STAT_FIELDS}
    for line in contents.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise ValueError(f"invalid cpu.stat line: {line!r}")
        name, raw_value = fields
        if name not in parsed:
            continue
        value = int(raw_value)
        if value < 0:
            raise ValueError(f"negative cpu.stat value for {name}")
        parsed[name] = value
    return parsed


def parse_io_stat(contents: str) -> dict[str, int]:
    parsed = {name: 0 for name in IO_STAT_FIELDS}
    for line in contents.splitlines():
        fields = line.split()
        if not fields or ":" not in fields[0]:
            raise ValueError(f"invalid io.stat line: {line!r}")
        for field in fields[1:]:
            if "=" not in field:
                raise ValueError(f"invalid io.stat field: {field!r}")
            name, raw_value = field.split("=", 1)
            if name not in parsed:
                continue
            value = int(raw_value)
            if value < 0:
                raise ValueError(f"negative io.stat value for {name}")
            parsed[name] += value
    return parsed


def _read_nonnegative_integer(path: Path) -> int:
    value = int(path.read_text(encoding="ascii").strip())
    if value < 0:
        raise ValueError(f"negative cgroup value in {path}")
    return value


def reset_memory_peak(cgroup: Path) -> None:
    (cgroup / "memory.peak").write_text("0\n", encoding="ascii")


@dataclass(frozen=True)
class CgroupMemorySnapshot:
    path: Path
    captured_ns: int
    current_bytes: int
    peak_bytes: int
    stats: dict[str, int]

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        captured_ns: int | None = None,
    ) -> "CgroupMemorySnapshot":
        return cls(
            path=path,
            captured_ns=time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
            if captured_ns is None
            else captured_ns,
            current_bytes=_read_nonnegative_integer(path / "memory.current"),
            peak_bytes=_read_nonnegative_integer(path / "memory.peak"),
            stats=parse_memory_stat((path / "memory.stat").read_text(encoding="ascii")),
        )

    def delta(self, baseline: "CgroupMemorySnapshot") -> "CgroupMemorySnapshot":
        if self.path != baseline.path:
            raise ValueError("memory deltas require snapshots from the same cgroup")
        return CgroupMemorySnapshot(
            path=self.path,
            captured_ns=self.captured_ns,
            current_bytes=self.current_bytes - baseline.current_bytes,
            peak_bytes=self.peak_bytes - baseline.peak_bytes,
            stats={
                name: self.stats.get(name, 0) - baseline.stats.get(name, 0)
                for name in MEMORY_STAT_FIELDS
            },
        )

    def to_columns(self, prefix: str) -> dict[str, int | str]:
        if not prefix:
            raise ValueError("snapshot column prefix must not be empty")
        columns: dict[str, int | str] = {
            f"{prefix}_cgroup_path": str(self.path),
            f"{prefix}_captured_ns": self.captured_ns,
            f"{prefix}_memory_current_bytes": self.current_bytes,
            f"{prefix}_memory_peak_bytes": self.peak_bytes,
        }
        columns.update(
            {
                f"{prefix}_memory_{name}_bytes": self.stats.get(name, 0)
                for name in MEMORY_STAT_FIELDS
            }
        )
        return columns


class CgroupMemoryTracker:
    CHECKPOINTS = (
        "worker_baseline",
        "prelaunch",
        "app_ready",
        "operation_complete",
        "held",
    )

    def __init__(self, path: Path):
        self.path = path
        self.snapshots: dict[str, CgroupMemorySnapshot] = {}

    def capture(self, checkpoint: str, *, capture=None) -> CgroupMemorySnapshot:
        if checkpoint not in self.CHECKPOINTS:
            raise ValueError(f"unknown memory checkpoint: {checkpoint}")
        if checkpoint in self.snapshots:
            raise ValueError(f"memory checkpoint already captured: {checkpoint}")
        expected = self.CHECKPOINTS[len(self.snapshots)]
        if checkpoint != expected:
            raise ValueError(f"expected {expected}, got {checkpoint}")
        snapshot = (
            CgroupMemorySnapshot.capture(self.path)
            if capture is None
            else capture()
        )
        if snapshot.path != self.path:
            raise ValueError("memory checkpoint belongs to a different cgroup")
        self.snapshots[checkpoint] = snapshot
        return snapshot

    def to_columns(self) -> dict[str, int | str]:
        missing = [name for name in self.CHECKPOINTS if name not in self.snapshots]
        if missing:
            raise ValueError("missing memory checkpoints: " + ", ".join(missing))
        baseline = self.snapshots["worker_baseline"]
        columns: dict[str, int | str] = {}
        for checkpoint in self.CHECKPOINTS:
            snapshot = self.snapshots[checkpoint]
            columns.update(snapshot.to_columns(checkpoint))
            if checkpoint != "worker_baseline":
                columns.update(snapshot.delta(baseline).to_columns(f"{checkpoint}_delta"))
        return columns


@dataclass(frozen=True)
class CgroupAccountingSnapshot:
    path: Path
    captured_ns: int
    cpu: dict[str, int]
    io: dict[str, int]

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        captured_ns: int | None = None,
    ) -> "CgroupAccountingSnapshot":
        return cls(
            path=path,
            captured_ns=time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
            if captured_ns is None
            else captured_ns,
            cpu=parse_cpu_stat((path / "cpu.stat").read_text(encoding="ascii")),
            io=parse_io_stat((path / "io.stat").read_text(encoding="ascii")),
        )

    def delta(self, baseline: "CgroupAccountingSnapshot") -> "CgroupAccountingSnapshot":
        if self.path != baseline.path:
            raise ValueError("accounting deltas require snapshots from the same cgroup")
        return CgroupAccountingSnapshot(
            path=self.path,
            captured_ns=self.captured_ns,
            cpu={name: self.cpu.get(name, 0) - baseline.cpu.get(name, 0) for name in CPU_STAT_FIELDS},
            io={name: self.io.get(name, 0) - baseline.io.get(name, 0) for name in IO_STAT_FIELDS},
        )

    def to_columns(self, prefix: str) -> dict[str, int | str]:
        if not prefix:
            raise ValueError("accounting column prefix must not be empty")
        columns: dict[str, int | str] = {
            f"{prefix}_accounting_cgroup_path": str(self.path),
            f"{prefix}_accounting_captured_ns": self.captured_ns,
        }
        columns.update(
            {f"{prefix}_cpu_{name}": self.cpu.get(name, 0) for name in CPU_STAT_FIELDS}
        )
        columns.update(
            {f"{prefix}_io_{name}": self.io.get(name, 0) for name in IO_STAT_FIELDS}
        )
        return columns


class CgroupAccountingTracker:
    CHECKPOINTS = CgroupMemoryTracker.CHECKPOINTS

    def __init__(self, path: Path):
        self.path = path
        self.snapshots: dict[str, CgroupAccountingSnapshot] = {}

    def capture(self, checkpoint: str, *, capture=None) -> CgroupAccountingSnapshot:
        if checkpoint not in self.CHECKPOINTS:
            raise ValueError(f"unknown accounting checkpoint: {checkpoint}")
        if checkpoint in self.snapshots:
            raise ValueError(f"accounting checkpoint already captured: {checkpoint}")
        expected = self.CHECKPOINTS[len(self.snapshots)]
        if checkpoint != expected:
            raise ValueError(f"expected {expected}, got {checkpoint}")
        snapshot = (
            CgroupAccountingSnapshot.capture(self.path)
            if capture is None
            else capture()
        )
        if snapshot.path != self.path:
            raise ValueError("accounting checkpoint belongs to a different cgroup")
        self.snapshots[checkpoint] = snapshot
        return snapshot

    def to_columns(self) -> dict[str, int | str]:
        missing = [name for name in self.CHECKPOINTS if name not in self.snapshots]
        if missing:
            raise ValueError("missing accounting checkpoints: " + ", ".join(missing))
        baseline = self.snapshots["worker_baseline"]
        columns: dict[str, int | str] = {}
        for checkpoint in self.CHECKPOINTS:
            snapshot = self.snapshots[checkpoint]
            columns.update(snapshot.to_columns(checkpoint))
            if checkpoint != "worker_baseline":
                columns.update(snapshot.delta(baseline).to_columns(f"{checkpoint}_delta"))
        return columns
