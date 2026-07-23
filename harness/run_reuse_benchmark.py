#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from benchmark_workloads import build_workload as build_workload_contract
from benchmark_workloads import parse_workload_output


ROOT = Path(
    os.environ.get("KUASAR_BENCH_ROOT", str(Path(__file__).resolve().parents[1]))
).resolve()
CANONICAL_ROOT = Path("/root/virtiolazyd/benchmark-results/20260720-canonical-extents")
TRANSPORT_RUNNER = Path(
    "/root/virtiolazyd/benchmark-results/20260721-integrated-accelerator/scripts/run_transport_benchmark.py"
)
BIN = ROOT / "bin"
RUNTIME = ROOT / "runtime"
LOGS = ROOT / "logs"
STORE_ROOT = CANONICAL_ROOT / "runtime" / "prep2" / "store-data"
MODE_PMEM = "lazy-pmem"
MODE_BLK = "vhost-user-blk"
MODE_BLK_SHARED = "vhost-user-blk-shared-cache"
MODES = (MODE_PMEM, MODE_BLK)
EVIDENCE_MODES = (MODE_BLK, MODE_BLK_SHARED, MODE_PMEM)
MODE_ID_TAGS = {
    MODE_BLK: "blk",
    MODE_BLK_SHARED: "shared",
    MODE_PMEM: "pmem",
}
GROUP_SIZES = (1, 2, 4, 8)
DEFAULT_WORKING_SET_BYTES = 128 * 1024 * 1024
READY_TIMEOUT_SECONDS = 180

OBSERVATION_MARKERS = (
    (b"lazy root prepared:", "lazy_prepare"),
    (b"CH started pid=", "ch_started"),
    (b"launch: launch_ack received", "launch_ack"),
    (b"KUASAR_APP_READY", "app_ready"),
    (b"KUASAR_REUSE_READ_BEGIN", "read_begin"),
    (b"KUASAR_REUSE_READ_END", "read_end"),
    (b"KUASAR_FIRST_TOUCH_BEGIN", "first_touch_begin"),
    (b"KUASAR_FIRST_TOUCH_END", "first_touch_end"),
    (b"KUASAR_REPEAT_READ_BEGIN", "repeat_read_begin"),
    (b"KUASAR_REPEAT_READ_END", "repeat_read_end"),
    (b"KUASAR_BENCH_APP_READY", "app_ready"),
    (b"KUASAR_BENCH_OPERATION_BEGIN", "read_begin"),
    (b"KUASAR_BENCH_OPERATION_END", "read_end"),
    (b"KUASAR_BENCH_READY", "ready"),
    (b"KUASAR_REUSE_READY", "ready"),
)
OBSERVATION_NAMES = tuple(name for _, name in OBSERVATION_MARKERS)


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def summarize(values: Iterable[float]) -> dict[str, float | int]:
    samples = list(values)
    if not samples:
        raise ValueError("summary requires at least one value")
    return {
        "count": len(samples),
        "mean": statistics.fmean(samples),
        "p50": percentile(samples, 0.50),
        "p90": percentile(samples, 0.90),
        "p95": percentile(samples, 0.95),
        "min": min(samples),
        "max": max(samples),
    }


def paired_comparison(rows: Iterable[dict[str, object]], metric: str) -> dict[str, float | int]:
    pairs: dict[int, dict[str, float]] = defaultdict(dict)
    for row in rows:
        pairs[int(row["round"])][str(row["mode"])] = float(row[metric])
    incomplete = [number for number, values in pairs.items() if set(values) != set(MODES)]
    if incomplete:
        raise ValueError(f"incomplete paired rounds: {incomplete}")
    pmem_wins = sum(values[MODE_PMEM] < values[MODE_BLK] for values in pairs.values())
    return {
        "pairs": len(pairs),
        "pmem_wins": pmem_wins,
        "win_rate_percent": pmem_wins / len(pairs) * 100,
    }


def observation_name(line: bytes) -> str | None:
    for marker, name in OBSERVATION_MARKERS:
        if marker in line:
            return name
    return None


def stage_seconds(observations_ns: dict[str, int], begin: str, end: str) -> float:
    missing = [name for name in (begin, end) if name not in observations_ns]
    if missing:
        raise RuntimeError("missing observation: " + ", ".join(missing))
    if observations_ns[end] < observations_ns[begin]:
        raise RuntimeError(f"observation {end} precedes {begin}")
    return (observations_ns[end] - observations_ns[begin]) / 1_000_000_000


def build_workload(mode: str, working_set_bytes: int, *, verify: bool = False) -> str:
    if working_set_bytes <= 0:
        raise ValueError("working_set_bytes must be positive")
    if mode == MODE_PMEM:
        device = "/dev/pmem1"
    elif mode in (MODE_BLK, MODE_BLK_SHARED):
        device = "/dev/vda"
    else:
        raise ValueError(f"unknown mode: {mode}")
    read_command = (
        f"head -c {working_set_bytes} {device} | sha256sum"
        if verify
        else f"head -c {working_set_bytes} {device} > /dev/null"
    )
    return "; ".join(
        [
            "set -eu",
            "sha256sum /bin/sh",
            "echo KUASAR_REUSE_READ_BEGIN",
            read_command,
            f"echo KUASAR_REUSE_BYTES={working_set_bytes}",
            "echo KUASAR_REUSE_READY",
            "sleep 600",
        ]
    )


def build_direct_workload(mode: str, working_set_bytes: int) -> str:
    block_bytes = 4096
    if working_set_bytes <= 0 or working_set_bytes % block_bytes:
        raise ValueError("direct working set must be a positive multiple of 4096")
    if mode == MODE_PMEM:
        device = "/dev/pmem1"
    elif mode in (MODE_BLK, MODE_BLK_SHARED):
        device = "/dev/vda"
    else:
        raise ValueError(f"unknown mode: {mode}")
    count = working_set_bytes // block_bytes
    read_command = (
        f"dd if={device} of=/dev/null bs={block_bytes} count={count} "
        "iflag=direct 2>/dev/null"
    )
    return "; ".join(
        [
            "set -eu",
            "sha256sum /bin/sh",
            read_command,
            "echo KUASAR_REUSE_READ_BEGIN",
            read_command,
            f"echo KUASAR_REUSE_BYTES={working_set_bytes}",
            "echo KUASAR_REUSE_READY",
            "sleep 600",
        ]
    )


def build_filesystem_workload() -> str:
    return "; ".join(
        [
            "set -eu",
            "sha256sum /bin/sh",
            "echo KUASAR_APP_READY",
            "echo KUASAR_REUSE_READ_BEGIN",
            "/opt/sandbox-runtime/bin/read-tree",
            "echo KUASAR_REUSE_READ_END",
            "echo KUASAR_REUSE_READY",
            "sleep 600",
        ]
    )


def build_first_touch_workload() -> str:
    return "; ".join(
        [
            "set -eu",
            "sha256sum /bin/sh",
            "echo KUASAR_APP_READY",
            "sleep 1",
            "echo KUASAR_FIRST_TOUCH_BEGIN",
            "/opt/sandbox-runtime/bin/read-tree",
            "echo KUASAR_FIRST_TOUCH_END",
            "sleep 1",
            "echo KUASAR_REPEAT_READ_BEGIN",
            "/opt/sandbox-runtime/bin/read-tree",
            "echo KUASAR_REPEAT_READ_END",
            "echo KUASAR_REUSE_READ_BEGIN",
            "echo KUASAR_REUSE_READ_END",
            "echo KUASAR_REUSE_READY",
            "sleep 600",
        ]
    )


def build_named_workload(name: str, *, hold_seconds: int = 600):
    return build_workload_contract(name, hold_seconds=hold_seconds)


def transport_launch_contract(
    mode: str,
    *,
    control_socket: Path | None,
    data_socket: Path | None,
    range_socket: Path,
    base_env: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    environment = dict(os.environ if base_env is None else base_env)
    if mode == MODE_BLK:
        return mode, environment
    if mode not in (MODE_PMEM, MODE_BLK_SHARED):
        raise ValueError(f"unknown mode: {mode}")
    if control_socket is None or data_socket is None:
        raise ValueError(f"{mode} requires lazyd sockets")
    return mode, environment


def load_runner():
    spec = importlib.util.spec_from_file_location("reuse_transport_runner", TRANSPORT_RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load transport runner: {TRANSPORT_RUNNER}")
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    runner.ROOT = ROOT
    runner.BIN = BIN
    runner.RUNTIME = RUNTIME
    runner.LOGS = LOGS
    runner.STORE_ROOT = STORE_ROOT
    return runner


def smaps_rollup(pid: int) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with Path(f"/proc/{pid}/smaps_rollup").open(encoding="utf-8") as stream:
            for line in stream:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                fields = rest.split()
                if fields and fields[0].isdigit():
                    values[name] = int(fields[0])
    except FileNotFoundError:
        return {}
    return values


def descendant_pids(root_pid: int) -> set[int]:
    found: set[int] = set()
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except FileNotFoundError:
            continue
        pending.extend(int(child) for child in children)
    return found


def process_family_pids(root_pid: int) -> set[int]:
    pids = descendant_pids(root_pid)
    try:
        process_group = os.getpgid(root_pid)
    except ProcessLookupError:
        return pids
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if os.getpgid(pid) == process_group:
                pids.add(pid)
        except (PermissionError, ProcessLookupError):
            continue
    return pids


def process_metrics(pids: Iterable[int]) -> dict[str, object]:
    rss = pss = private_dirty = 0
    commands: list[str] = []
    live_pids: list[int] = []
    for pid in sorted(set(pids)):
        rollup = smaps_rollup(pid)
        if not rollup:
            continue
        live_pids.append(pid)
        rss += rollup.get("Rss", 0)
        pss += rollup.get("Pss", 0)
        private_dirty += rollup.get("Private_Dirty", 0)
        try:
            command = Path(f"/proc/{pid}/comm").read_text().strip()
        except FileNotFoundError:
            command = "unknown"
        commands.append(f"{pid}:{command}")
    return {
        "pids": live_pids,
        "commands": commands,
        "process_count": len(live_pids),
        "rss_kib": rss,
        "pss_kib": pss,
        "private_dirty_kib": private_dirty,
    }


def process_family_metrics(*root_pids: int) -> dict[str, object]:
    pids: set[int] = set()
    for root_pid in root_pids:
        pids.update(process_family_pids(root_pid))
    return process_metrics(pids)


def cache_snapshot(cache_root: Path) -> dict[str, int | str]:
    layers = list(cache_root.rglob("layer.erofs"))
    bitmaps = list(cache_root.rglob("layer.erofs.bitmap"))
    if len(layers) != 1 or len(bitmaps) != 1:
        raise RuntimeError(
            f"expected one shared cache, got layers={len(layers)} bitmaps={len(bitmaps)}"
        )
    layer, bitmap = layers[0], bitmaps[0]
    layer_stat, bitmap_stat = layer.stat(), bitmap.stat()
    return {
        "cache_path": str(layer),
        "cache_dev": f"{os.major(layer_stat.st_dev):02x}:{os.minor(layer_stat.st_dev):02x}",
        "cache_inode": layer_stat.st_ino,
        "cache_blocks": layer_stat.st_blocks,
        "cache_mtime_ns": layer_stat.st_mtime_ns,
        "bitmap_blocks": bitmap_stat.st_blocks,
        "bitmap_mtime_ns": bitmap_stat.st_mtime_ns,
    }


def assert_cache_unchanged(current: dict[str, int | str], baseline: dict[str, int | str]) -> None:
    fields = (
        "cache_dev",
        "cache_inode",
        "cache_blocks",
        "cache_mtime_ns",
        "bitmap_blocks",
        "bitmap_mtime_ns",
    )
    changed = [field for field in fields if current[field] != baseline[field]]
    if changed:
        raise RuntimeError("shared lazy cache changed after prewarm: " + ", ".join(changed))


class HeldVM:
    def __init__(
        self,
        runner,
        base,
        context,
        *,
        sample_id: str,
        mode: str,
        index: int,
        config: Path,
        runtime_dir: Path,
        log_dir: Path,
        socket_dir: Path,
        process_env: dict[str, str] | None = None,
        workload_name: str | None = None,
        workload_result_kind: str | None = None,
    ):
        self.runner = runner
        self.base = base
        self.mode = mode
        self.index = index
        self.runtime_dir = runtime_dir
        self.log_dir = log_dir
        self.socket_dir = socket_dir
        self.workload_name = workload_name
        self.workload_result_kind = workload_result_kind
        self.start_ns = runner.now_ns()
        self.read_begin_ns: int | None = None
        self.ready_ns: int | None = None
        self.ch_pid: int | None = None
        self.application_hash: str | None = None
        self.workload_result_sha256: str | None = None
        self.workload_capability: str | None = None
        self.workload_parse_error: str | None = None
        self.lower_hash: str | None = None
        self.data_bytes: int | None = None
        self.observations_ns: dict[str, int] = {}
        self.observation_events = {name: threading.Event() for name in OBSERVATION_NAMES}
        self.ready = threading.Event()
        command = [
            str(BIN / "sandbox-ctl"),
            "run",
            "-config",
            str(config),
            "-manifest-config",
            str(context.manifest_config),
            "-sandbox-id",
            sample_id,
            "-run-root",
            str(socket_dir / "run-root"),
            "-base-root",
            str(runtime_dir / "base-root"),
            "-ch-binary",
            str(BIN / "cloud-hypervisor"),
            "-console",
            "file=" + str(log_dir / "guest-console.log"),
            "-stats-interval",
            "0",
            "-stats-json",
            str(log_dir / "stats.json"),
            "-ping-fatal-threshold",
            "0",
        ]
        (log_dir / "sandbox-command.json").write_text(
            json.dumps(command, indent=2) + "\n", encoding="utf-8"
        )
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=process_env,
        )
        self.threads = [
            threading.Thread(
                target=self._reader,
                args=(self.process.stdout, log_dir / "sandbox.stdout.log"),
                daemon=True,
            ),
            threading.Thread(
                target=self._reader,
                args=(self.process.stderr, log_dir / "sandbox.stderr.log"),
                daemon=True,
            ),
        ]
        for thread in self.threads:
            thread.start()

    def _reader(self, source, destination: Path) -> None:
        app_pattern = re.compile(rb"^([0-9a-f]{64})[ \t]+/bin/sh\s*$")
        lower_pattern = re.compile(rb"^([0-9a-f]{64})[ \t]+-\s*$")
        pid_pattern = re.compile(rb"CH started pid=([0-9]+)")
        bytes_pattern = re.compile(rb"KUASAR_REUSE_BYTES=([0-9]+)")
        with destination.open("wb") as output:
            while True:
                line = source.readline()
                if not line:
                    return
                timestamp = self.runner.now_ns()
                output.write(line)
                output.flush()
                if match := app_pattern.match(line.strip()):
                    self.application_hash = match.group(1).decode()
                if match := lower_pattern.match(line.strip()):
                    self.lower_hash = match.group(1).decode()
                if match := pid_pattern.search(line):
                    self.ch_pid = int(match.group(1))
                if match := bytes_pattern.search(line):
                    self.data_bytes = int(match.group(1))
                try:
                    workload_field, workload_value = parse_workload_output(line)
                except (UnicodeDecodeError, ValueError) as error:
                    self.workload_parse_error = str(error)
                    workload_field = workload_value = None
                if workload_field == "result_sha256":
                    self.workload_result_sha256 = str(workload_value)
                elif workload_field == "data_bytes":
                    self.data_bytes = int(workload_value)  # type: ignore[arg-type]
                elif workload_field == "capability":
                    self.workload_capability = str(workload_value)
                name = observation_name(line)
                if name:
                    if name not in self.observations_ns:
                        self.observations_ns[name] = timestamp
                        self.observation_events[name].set()
                if name == "read_begin":
                    self.read_begin_ns = timestamp
                if name == "ready":
                    self.ready_ns = timestamp
                    self.ready.set()

    def wait_ready(self, timeout: int = READY_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"{self.mode} VM {self.index} exited before ready: {self.process.returncode}"
                )
            if self.ready.wait(0.1):
                break
        else:
            raise TimeoutError(f"{self.mode} VM {self.index} did not reach ready marker")
        if self.workload_parse_error is not None:
            raise RuntimeError(
                f"{self.mode} VM {self.index} emitted invalid workload output: "
                f"{self.workload_parse_error}"
            )
        required = (self.ch_pid, self.read_begin_ns, self.ready_ns)
        if self.workload_name is None:
            required += (self.application_hash, self.data_bytes)
        elif self.workload_result_kind == "sha256":
            required += (self.workload_result_sha256, self.data_bytes)
        elif self.workload_result_kind == "byte-count":
            required += (self.data_bytes,)
        elif self.workload_result_kind == "capability":
            required += (self.workload_capability,)
        else:
            raise RuntimeError(f"unknown workload result kind: {self.workload_result_kind}")
        if None in required:
            raise RuntimeError(f"{self.mode} VM {self.index} missed required observations")
        if self.data_bytes is not None and self.data_bytes <= 0:
            raise RuntimeError(f"{self.mode} VM {self.index} read an empty workload")

    def wait_observation(self, name: str, timeout: int = READY_TIMEOUT_SECONDS) -> int:
        if name not in self.observation_events:
            raise ValueError(f"unknown observation: {name}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"{self.mode} VM {self.index} exited before observation {name}: "
                    f"{self.process.returncode}"
                )
            if self.observation_events[name].wait(0.1):
                return self.observations_ns[name]
        raise TimeoutError(f"{self.mode} VM {self.index} missed observation {name}")

    def stage_seconds(self, begin: str, end: str) -> float:
        return stage_seconds(self.observations_ns, begin, end)

    def since_start_seconds(self, observation: str) -> float:
        if observation not in self.observations_ns:
            raise RuntimeError(f"missing observation: {observation}")
        if self.observations_ns[observation] < self.start_ns:
            raise RuntimeError(f"observation {observation} precedes VM start")
        return (self.observations_ns[observation] - self.start_ns) / 1_000_000_000

    @property
    def ready_seconds(self) -> float:
        return (self.ready_ns - self.start_ns) / 1_000_000_000  # type: ignore[operator]

    @property
    def read_seconds(self) -> float:
        return (self.ready_ns - self.read_begin_ns) / 1_000_000_000  # type: ignore[operator]

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                self.base.terminate_group(self.process)
        for thread in self.threads:
            thread.join(timeout=5)
        if self.socket_dir.exists():
            shutil.rmtree(self.socket_dir)
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)


def start_vm(
    runner,
    base,
    context,
    image,
    *,
    phase: str,
    mode: str,
    index: int,
    tap: str,
    range_socket: Path,
    control_socket: Path | None,
    data_socket: Path | None,
    working_set_bytes: int,
    verify: bool,
    workload_kind: str = "raw",
    workload_name: str | None = None,
) -> HeldVM:
    sample_id = f"{phase}-{MODE_ID_TAGS.get(mode, mode)}-{index}"
    runtime_dir = RUNTIME / phase / sample_id
    log_dir = LOGS / phase / sample_id
    socket_dir = base.short_socket_dir(sample_id)
    for path in (runtime_dir, log_dir, socket_dir):
        path.mkdir(parents=True, exist_ok=False)
    config = log_dir / "sandbox.yaml"
    workload_result_kind = None
    if workload_name is not None:
        workload = build_named_workload(workload_name)
        if workload.image_name != image.name:
            raise ValueError(
                f"workload {workload_name} requires image {workload.image_name}, got {image.name}"
            )
        workload_command = workload.command
        workload_result_kind = workload.result_kind
    elif workload_kind == "raw":
        workload_command = build_workload(mode, working_set_bytes, verify=verify)
    elif workload_kind == "direct":
        workload_command = build_direct_workload(mode, working_set_bytes)
    elif workload_kind == "filesystem":
        workload_command = build_filesystem_workload()
    elif workload_kind == "first-touch":
        workload_command = build_first_touch_workload()
    else:
        raise ValueError(f"unknown workload kind: {workload_kind}")
    config_mode, process_env = transport_launch_contract(
        mode,
        control_socket=control_socket,
        data_socket=data_socket,
        range_socket=range_socket,
    )
    runner.write_sandbox_config(
        config,
        mode=config_mode,
        image=image,
        tap=tap,
        upper=runtime_dir / "upper.ext4",
        kernel=BIN / "vmlinux",
        runtime=BIN / "sandbox-runtime.erofs",
        upper_template=context.upper_template,
        range_socket=range_socket,
        control_socket=control_socket,
        data_socket=data_socket,
        workload_command=workload_command,
    )
    return HeldVM(
        runner,
        base,
        context,
        sample_id=sample_id,
        mode=mode,
        index=index,
        config=config,
        runtime_dir=runtime_dir,
        log_dir=log_dir,
        socket_dir=socket_dir,
        process_env=process_env,
        workload_name=workload_name,
        workload_result_kind=workload_result_kind,
    )


def selected_cache_smaps(base, pid: int, vm_index: int, group_vms: int) -> list[dict[str, object]]:
    try:
        return base.selected_smaps(pid, vm_index, group_vms)
    except RuntimeError:
        pass

    sections: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    with Path(f"/proc/{pid}/smaps").open(encoding="utf-8") as stream:
        for raw in stream:
            line = raw.rstrip("\n")
            header = base.SMAPS_HEADER.match(line)
            if header:
                if current is not None and "memfd:lazyd-erofs-" in str(current["path"]):
                    sections.append(current)
                current = {
                    "group_vms": group_vms,
                    "vm_index": vm_index,
                    "ch_pid": pid,
                    "start": header.group(1),
                    "end": header.group(2),
                    "perms": header.group(3),
                    "offset": int(header.group(4), 16),
                    "dev": header.group(5),
                    "inode": int(header.group(6)),
                    "path": header.group(7),
                    "rss_kib": 0,
                    "pss_kib": 0,
                    "shared_clean_kib": 0,
                    "private_clean_kib": 0,
                    "private_dirty_kib": 0,
                }
                continue
            if current is None:
                continue
            for prefix, field in (
                ("Rss:", "rss_kib"),
                ("Pss:", "pss_kib"),
                ("Shared_Clean:", "shared_clean_kib"),
                ("Private_Clean:", "private_clean_kib"),
                ("Private_Dirty:", "private_dirty_kib"),
            ):
                if line.startswith(prefix):
                    current[field] = int(line.split()[1])
                    break
        if current is not None and "memfd:lazyd-erofs-" in str(current["path"]):
            sections.append(current)
    if not sections:
        raise RuntimeError(f"no lazy cache mappings found in /proc/{pid}/smaps")
    return sections


def selected_mapping_totals(base, vms: list[HeldVM]) -> dict[str, object]:
    rss = pss = shared_clean = private_dirty = 0
    identities: set[tuple[str, int]] = set()
    for vm in vms:
        sections = selected_cache_smaps(base, vm.ch_pid, vm.index, len(vms))
        for section in sections:
            rss += int(section["rss_kib"])
            pss += int(section["pss_kib"])
            shared_clean += int(section["shared_clean_kib"])
            private_dirty += int(section["private_dirty_kib"])
            identities.add((str(section["dev"]), int(section["inode"])))
    return {
        "mapped_rss_kib": rss,
        "mapped_pss_kib": pss,
        "mapped_shared_clean_kib": shared_clean,
        "mapped_private_dirty_kib": private_dirty,
        "mapping_identities": sorted(identities),
    }


def measure_vm(vm: HeldVM, lazyd_process: subprocess.Popen[bytes] | None) -> dict[str, object]:
    family = process_family_metrics(vm.process.pid, vm.ch_pid)
    service = smaps_rollup(lazyd_process.pid) if lazyd_process is not None else {}
    return {
        "ready_seconds": vm.ready_seconds,
        "read_seconds": vm.read_seconds,
        "sandbox_process_count": family["process_count"],
        "sandbox_family_rss_kib": family["rss_kib"],
        "sandbox_family_pss_kib": family["pss_kib"],
        "sandbox_family_private_dirty_kib": family["private_dirty_kib"],
        "lazyd_pss_kib": service.get("Pss", 0),
        "total_measured_pss_kib": int(family["pss_kib"]) + service.get("Pss", 0),
        "processes": ",".join(family["commands"]),
        "application_hash": vm.application_hash,
        "lower_hash": vm.lower_hash or "",
        "data_bytes": vm.data_bytes,
    }


def run_correctness_smoke(runner, base, image, working_set_bytes: int) -> list[dict[str, object]]:
    phase = "correctness-smoke"
    rows: list[dict[str, object]] = []
    lazyd_process = None
    shared_sockets = base.short_socket_dir(phase)
    shared_sockets.mkdir(parents=True, exist_ok=False)
    service_runtime = RUNTIME / phase / "lazyd-service"
    service_logs = LOGS / phase / "lazyd-service"
    service_runtime.mkdir(parents=True, exist_ok=False)
    service_logs.mkdir(parents=True, exist_ok=False)
    try:
        with base.PhaseContext(phase, 1) as context:
            lazyd_process, _, control_socket, data_socket = base.start_lazyd(
                service_runtime, shared_sockets, service_logs, None
            )
            for index, mode in enumerate(MODES, start=1):
                vm = start_vm(
                    runner,
                    base,
                    context,
                    image,
                    phase=phase,
                    mode=mode,
                    index=index,
                    tap=context.taps[0],
                    range_socket=context.range_socket,
                    control_socket=control_socket if mode == MODE_PMEM else None,
                    data_socket=data_socket if mode == MODE_PMEM else None,
                    working_set_bytes=working_set_bytes,
                    verify=True,
                )
                try:
                    vm.wait_ready()
                    rows.append({"mode": mode, **measure_vm(vm, lazyd_process if mode == MODE_PMEM else None)})
                finally:
                    vm.stop()
    finally:
        base.close_backend_process(lazyd_process)
        if shared_sockets.exists():
            shutil.rmtree(shared_sockets)
        if service_runtime.exists():
            shutil.rmtree(service_runtime)
    if len({row["application_hash"] for row in rows}) != 1:
        raise RuntimeError("application hashes differ across transports")
    if len({row["lower_hash"] for row in rows}) != 1:
        raise RuntimeError("lower-device hashes differ across transports")
    return rows


def run_latency(runner, base, image, rounds: int, working_set_bytes: int) -> list[dict[str, object]]:
    phase = "warm-latency"
    rows: list[dict[str, object]] = []
    lazyd_process = None
    baseline_cache: dict[str, int | str] | None = None
    shared_sockets = base.short_socket_dir(phase)
    shared_sockets.mkdir(parents=True, exist_ok=False)
    service_runtime = RUNTIME / phase / "lazyd-service"
    service_logs = LOGS / phase / "lazyd-service"
    service_runtime.mkdir(parents=True, exist_ok=False)
    service_logs.mkdir(parents=True, exist_ok=False)
    try:
        with base.PhaseContext(phase, 1) as context:
            lazyd_process, cache_root, control_socket, data_socket = base.start_lazyd(
                service_runtime, shared_sockets, service_logs, None
            )
            for warm_index, mode in enumerate((MODE_BLK, MODE_PMEM), start=1):
                vm = start_vm(
                    runner,
                    base,
                    context,
                    image,
                    phase=phase,
                    mode=mode,
                    index=-warm_index,
                    tap=context.taps[0],
                    range_socket=context.range_socket,
                    control_socket=control_socket if mode == MODE_PMEM else None,
                    data_socket=data_socket if mode == MODE_PMEM else None,
                    working_set_bytes=working_set_bytes,
                    verify=False,
                    workload_kind="direct",
                )
                try:
                    vm.wait_ready()
                finally:
                    vm.stop()
            baseline_cache = cache_snapshot(cache_root)

            for round_number in range(1, rounds + 1):
                order = MODES if round_number % 2 else tuple(reversed(MODES))
                for execution_order, mode in enumerate(order, start=1):
                    vm = start_vm(
                        runner,
                        base,
                        context,
                        image,
                        phase=phase,
                        mode=mode,
                        index=round_number * 10 + execution_order,
                        tap=context.taps[0],
                        range_socket=context.range_socket,
                        control_socket=control_socket if mode == MODE_PMEM else None,
                        data_socket=data_socket if mode == MODE_PMEM else None,
                        working_set_bytes=working_set_bytes,
                        verify=False,
                        workload_kind="direct",
                    )
                    try:
                        vm.wait_ready()
                        time.sleep(0.2)
                        row = {
                            "round": round_number,
                            "execution_order": execution_order,
                            "mode": mode,
                            **measure_vm(vm, lazyd_process if mode == MODE_PMEM else None),
                        }
                        rows.append(row)
                        print(
                            f"latency round={round_number}/{rounds} mode={mode} "
                            f"ready={row['ready_seconds']:.6f}s read={row['read_seconds']:.6f}s "
                            f"pss={float(row['total_measured_pss_kib']) / 1024:.1f}MiB",
                            flush=True,
                        )
                    finally:
                        vm.stop()
                    if mode == MODE_PMEM:
                        assert_cache_unchanged(cache_snapshot(cache_root), baseline_cache)
    finally:
        base.close_backend_process(lazyd_process)
        if shared_sockets.exists():
            shutil.rmtree(shared_sockets)
        if service_runtime.exists():
            shutil.rmtree(service_runtime)
    return rows


def run_scale_mode(
    runner,
    base,
    image,
    mode: str,
    working_set_bytes: int,
    *,
    group_sizes: tuple[int, ...] = GROUP_SIZES,
    phase: str | None = None,
) -> list[dict[str, object]]:
    if not group_sizes or any(size <= 0 for size in group_sizes):
        raise ValueError("group sizes must be positive")
    if tuple(sorted(set(group_sizes))) != group_sizes:
        raise ValueError("group sizes must be unique and increasing")
    phase = phase or f"scale-{mode}"
    rows: list[dict[str, object]] = []
    vms: list[HeldVM] = []
    lazyd_process = None
    cache_root: Path | None = None
    baseline_cache: dict[str, int | str] | None = None
    shared_sockets = base.short_socket_dir(phase)
    shared_sockets.mkdir(parents=True, exist_ok=False)
    service_runtime = RUNTIME / phase / "lazyd-service"
    service_logs = LOGS / phase / "lazyd-service"
    service_runtime.mkdir(parents=True, exist_ok=False)
    service_logs.mkdir(parents=True, exist_ok=False)
    try:
        with base.PhaseContext(phase, max(group_sizes)) as context:
            if mode == MODE_PMEM:
                lazyd_process, cache_root, control_socket, data_socket = base.start_lazyd(
                    service_runtime, shared_sockets, service_logs, None
                )
            else:
                control_socket = data_socket = None

            warm_vm = start_vm(
                runner,
                base,
                context,
                image,
                phase=phase,
                mode=mode,
                index=-1,
                tap=context.taps[0],
                range_socket=context.range_socket,
                control_socket=control_socket,
                data_socket=data_socket,
                working_set_bytes=working_set_bytes,
                verify=False,
                workload_kind="filesystem",
            )
            try:
                warm_vm.wait_ready()
            finally:
                warm_vm.stop()
            if cache_root is not None:
                baseline_cache = cache_snapshot(cache_root)

            for index in range(1, max(group_sizes) + 1):
                vm = start_vm(
                    runner,
                    base,
                    context,
                    image,
                    phase=phase,
                    mode=mode,
                    index=index,
                    tap=context.taps[index - 1],
                    range_socket=context.range_socket,
                    control_socket=control_socket,
                    data_socket=data_socket,
                    working_set_bytes=working_set_bytes,
                    verify=False,
                    workload_kind="filesystem",
                )
                vms.append(vm)
                vm.wait_ready()
                time.sleep(0.5)
                if index not in group_sizes:
                    continue
                families = [
                    process_family_metrics(active.process.pid, active.ch_pid) for active in vms
                ]
                service_pss = smaps_rollup(lazyd_process.pid).get("Pss", 0) if lazyd_process else 0
                row: dict[str, object] = {
                    "mode": mode,
                    "vm_count": index,
                    "working_set_bytes_per_vm": vms[0].data_bytes,
                    "mean_ready_seconds": statistics.fmean(active.ready_seconds for active in vms),
                    "mean_read_seconds": statistics.fmean(active.read_seconds for active in vms),
                    "latest_ready_seconds": vms[-1].ready_seconds,
                    "latest_read_seconds": vms[-1].read_seconds,
                    "sandbox_process_count": sum(int(item["process_count"]) for item in families),
                    "sandbox_family_rss_kib": sum(int(item["rss_kib"]) for item in families),
                    "sandbox_family_pss_kib": sum(int(item["pss_kib"]) for item in families),
                    "lazyd_pss_kib": service_pss,
                    "total_measured_pss_kib": sum(int(item["pss_kib"]) for item in families) + service_pss,
                    "application_hash": vms[0].application_hash,
                    "cache_dev": "",
                    "cache_inode": "",
                    "cache_blocks": "",
                    "cache_mtime_ns": "",
                    "bitmap_blocks": "",
                    "bitmap_mtime_ns": "",
                    "mapped_rss_kib": 0,
                    "mapped_pss_kib": 0,
                    "mapped_shared_clean_kib": 0,
                    "mapped_private_dirty_kib": 0,
                    "mapping_identities": "",
                    "cache_path": "",
                }
                if cache_root is not None and baseline_cache is not None:
                    snapshot = cache_snapshot(cache_root)
                    assert_cache_unchanged(snapshot, baseline_cache)
                    mappings = selected_mapping_totals(base, vms)
                    if len(mappings["mapping_identities"]) != 1:
                        raise RuntimeError(
                            f"PMEM VMs mapped different cache files: {mappings['mapping_identities']}"
                        )
                    row.update(snapshot)
                    row.update(mappings)
                    row["mapping_identities"] = json.dumps(row["mapping_identities"])
                rows.append(row)
                print(
                    f"scale mode={mode} vms={index} latest_read={row['latest_read_seconds']:.6f}s "
                    f"total_pss={float(row['total_measured_pss_kib']) / 1024:.1f}MiB",
                    flush=True,
                )
    finally:
        for vm in reversed(vms):
            vm.stop()
        base.close_backend_process(lazyd_process)
        if shared_sockets.exists():
            shutil.rmtree(shared_sockets)
        if service_runtime.exists():
            shutil.rmtree(service_runtime)
    return rows


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise RuntimeError(f"refusing to write empty result set: {path}")
    if path.exists():
        raise RuntimeError(f"refusing to overwrite existing results: {path}")
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_tsv(path: Path) -> list[dict[str, object]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def improvement_percent(pmem: float, blk: float) -> float:
    return (blk - pmem) / blk * 100


def analyze_results() -> dict[str, object]:
    latency_rows = read_tsv(ROOT / "latency-results.tsv")
    scale_rows = read_tsv(ROOT / "scale-results.tsv")
    latency: dict[str, object] = {}
    for metric in ("ready_seconds", "read_seconds", "total_measured_pss_kib"):
        by_mode = {
            mode: summarize(float(row[metric]) for row in latency_rows if row["mode"] == mode)
            for mode in MODES
        }
        latency[metric] = {
            "by_mode": by_mode,
            "paired": paired_comparison(latency_rows, metric),
            "p50_improvement_percent": improvement_percent(
                float(by_mode[MODE_PMEM]["p50"]), float(by_mode[MODE_BLK]["p50"])
            ),
            "p95_improvement_percent": improvement_percent(
                float(by_mode[MODE_PMEM]["p95"]), float(by_mode[MODE_BLK]["p95"])
            ),
        }
    scale: list[dict[str, object]] = []
    for vm_count in GROUP_SIZES:
        values = {
            str(row["mode"]): row
            for row in scale_rows
            if int(row["vm_count"]) == vm_count
        }
        pmem, blk = values[MODE_PMEM], values[MODE_BLK]
        scale.append(
            {
                "vm_count": vm_count,
                "pmem_latest_read_seconds": float(pmem["latest_read_seconds"]),
                "blk_latest_read_seconds": float(blk["latest_read_seconds"]),
                "read_improvement_percent": improvement_percent(
                    float(pmem["latest_read_seconds"]), float(blk["latest_read_seconds"])
                ),
                "pmem_total_pss_kib": int(pmem["total_measured_pss_kib"]),
                "blk_total_pss_kib": int(blk["total_measured_pss_kib"]),
                "pss_improvement_percent": improvement_percent(
                    float(pmem["total_measured_pss_kib"]), float(blk["total_measured_pss_kib"])
                ),
                "pmem_mapped_rss_kib": int(pmem["mapped_rss_kib"]),
                "pmem_mapped_pss_kib": int(pmem["mapped_pss_kib"]),
                "pmem_mapped_shared_clean_kib": int(pmem["mapped_shared_clean_kib"]),
            }
        )
    result = {"latency": latency, "scale": scale}
    (ROOT / "analysis.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def ensure_fresh_path(path: Path) -> None:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite existing path: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("smoke", "latency", "scale", "analyze"))
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--working-set-mib", type=int, default=128)
    args = parser.parse_args()
    if args.rounds <= 0 or args.working_set_mib <= 0:
        raise RuntimeError("rounds and working-set-mib must be positive")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    if args.phase == "analyze":
        print(json.dumps(analyze_results(), indent=2))
        return 0

    runner = load_runner()
    base = runner.load_base_runner()
    image = next(item for item in runner.load_images() if item.name == "openeuler-24.03-lts")
    working_set_bytes = args.working_set_mib * 1024 * 1024
    if working_set_bytes > image.erofs_data_bytes:
        raise RuntimeError("working set exceeds EROFS data size")

    if args.phase == "smoke":
        output = ROOT / "smoke-results.tsv"
        ensure_fresh_path(output)
        rows = run_correctness_smoke(runner, base, image, working_set_bytes)
    elif args.phase == "latency":
        output = ROOT / "latency-results.tsv"
        ensure_fresh_path(output)
        rows = run_latency(runner, base, image, args.rounds, working_set_bytes)
    else:
        output = ROOT / "scale-results.tsv"
        ensure_fresh_path(output)
        rows = run_scale_mode(runner, base, image, MODE_PMEM, working_set_bytes)
        rows.extend(run_scale_mode(runner, base, image, MODE_BLK, working_set_bytes))
        if len({row["working_set_bytes_per_vm"] for row in rows}) != 1:
            raise RuntimeError("filesystem workload byte counts differ across transports")
    write_tsv(output, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
