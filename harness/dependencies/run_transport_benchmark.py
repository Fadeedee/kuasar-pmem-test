#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
BASE_ROOT = Path("/root/virtiolazyd/benchmark-results/20260720-canonical-extents")
BIN = Path("/root/virtiolazyd/benchmark-results/20260721-blk-pmem/bin")
INTEGRATED_MANIFEST_CTL = ROOT / "bin" / "manifest-ctl-integrated"
RUNTIME = ROOT / "runtime"
LOGS = ROOT / "logs"
STORE_ROOT = RUNTIME / "prep2" / "store-data"
BASE_RUNNER_PATH = BASE_ROOT / "scripts" / "run-performance.py"
MODE_LAZY = "lazy-pmem"
MODE_BLK = "vhost-user-blk"
MODE_BLK_SHARED = "vhost-user-blk-shared-cache"
MODES = (MODE_LAZY, MODE_BLK)
ALIGNMENT_BYTES = 2 * 1024 * 1024
MATERIALIZATION_MAX_BYTES = 1024 * 1024
VM_TIMEOUT_SECONDS = 600
RAW_CLOCK = time.CLOCK_MONOTONIC_RAW


@dataclass(frozen=True)
class Image:
    name: str
    oci_ref: str
    source_digest: str
    resolved_digest: str
    manifest_key: str
    erofs_data_bytes: int
    manifest_image_bytes: int
    pmem_bytes: int
    chunk_count: int


@dataclass(frozen=True)
class Sample:
    image: Image
    mode: str
    round_number: int
    execution_order: int
    prepare_seconds: float
    guest_ready_seconds: float
    application_ready_seconds: float
    sequential_read_seconds: float
    end_to_end_seconds: float
    prepare_allocated_bytes: int
    application_ready_allocated_bytes: int
    final_allocated_bytes: int
    application_hash: str
    lower_hash: str
    lower_read_count: int | None
    lower_read_bytes: int | None
    log_path: str


def load_base_runner():
    spec = importlib.util.spec_from_file_location("canonical_benchmark_base", BASE_RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load base runner: {BASE_RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ROOT = ROOT
    module.BIN = BIN
    module.SCRIPTS = BASE_ROOT / "scripts"
    module.RUNTIME = RUNTIME
    module.LOGS = LOGS
    module.STORE_ROOT = STORE_ROOT
    module.VM_TIMEOUT_SECONDS = VM_TIMEOUT_SECONDS
    return module


def now_ns() -> int:
    return time.clock_gettime_ns(RAW_CLOCK)


def seconds(start: int, end: int) -> float:
    return (end - start) / 1_000_000_000


def load_images(names: set[str] | None = None) -> list[Image]:
    images: list[Image] = []
    with (ROOT / "images.tsv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            if names and row["image"] not in names:
                continue
            images.append(
                Image(
                    name=row["image"],
                    oci_ref=row["oci_ref"],
                    source_digest=row["source_digest"],
                    resolved_digest=row["resolved_platform_digest"],
                    manifest_key=row["manifest_key"],
                    erofs_data_bytes=int(row["erofs_data_bytes"]),
                    manifest_image_bytes=int(row["manifest_image_bytes"]),
                    pmem_bytes=int(row["pmem_bytes"]),
                    chunk_count=int(row["chunk_count"]),
                )
            )
    if not images:
        raise RuntimeError("no benchmark images selected")
    if names:
        missing = names - {image.name for image in images}
        if missing:
            raise RuntimeError("unknown images: " + ", ".join(sorted(missing)))
    return images


def quote(value: str) -> str:
    return json.dumps(value)


def workload(mode: str, erofs_data_bytes: int) -> str:
    if mode == MODE_LAZY:
        lower_device = "/dev/pmem1"
    elif mode in (MODE_BLK, MODE_BLK_SHARED):
        lower_device = "/dev/vda"
    else:
        raise ValueError(f"unknown mode: {mode}")
    return "; ".join(
        [
            "set -eu",
            "sha256sum /bin/sh",
            "echo KUASAR_APP_READY",
            "echo KUASAR_READ_BEGIN",
            f"head -c {erofs_data_bytes} {lower_device} | sha256sum",
            "echo KUASAR_READ_END",
        ]
    )


def write_sandbox_config(
    path: Path,
    *,
    mode: str,
    image: Image,
    tap: str,
    upper: Path,
    kernel: Path,
    runtime: Path,
    upper_template: Path,
    range_socket: Path,
    control_socket: Path | None,
    data_socket: Path | None,
    workload_command: str | None = None,
) -> None:
    lines = [
        "resources:",
        "  capacity: { cpu: 1, memory: 512MiB }",
        "  allocatable: { cpu: 1, memory: 512MiB }",
        "network:",
        f"  tap: {quote(tap)}",
        '  ip: ""',
        '  hostname: "transport-benchmark"',
        "boot:",
        f"  kernel: {quote('file://' + str(kernel))}",
        f"  runtime: {quote('file://' + str(runtime))}",
        "  root:",
        f"    base: {quote('manifest://' + image.manifest_key)}",
    ]
    if mode in (MODE_LAZY, MODE_BLK_SHARED):
        if control_socket is None or data_socket is None:
            raise ValueError(f"{mode} requires control and data sockets")
        lower_device = MODE_LAZY if mode == MODE_LAZY else MODE_BLK
        lines.extend(
            [
                f"    lower_device: {lower_device}",
                "    overlay:",
                f"      diff: {quote('file://' + str(upper))}",
                f"      diff_template: {quote('file://' + str(upper_template))}",
                "    lazy_cache:",
                f"      lazyd_control_socket: {quote(str(control_socket))}",
                f"      lazyd_data_socket: {quote(str(data_socket))}",
                f"      accelerator_socket: {quote(str(range_socket))}",
                f"      materialization_max_bytes: {MATERIALIZATION_MAX_BYTES}",
                f"      alignment_bytes: {ALIGNMENT_BYTES}",
            ]
        )
    elif mode == MODE_BLK:
        lines.extend(
            [
                "    lower_device: vhost-user-blk",
                "    overlay:",
                f"      diff: {quote('file://' + str(upper))}",
                f"      diff_template: {quote('file://' + str(upper_template))}",
            ]
        )
    else:
        raise ValueError(f"unknown mode: {mode}")
    lines.extend(
        [
            "launch:",
            '  exec: "/bin/sh"',
            f"  args: {json.dumps(['-c', workload_command or workload(mode, image.erofs_data_bytes)])}",
            "  restart: never",
            "  start_timeout: 120s",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def wait_vm(
    *,
    base,
    context,
    sample_id: str,
    config: Path,
    log_dir: Path,
    run_root: Path,
    sandbox_base_root: Path,
    t0: int,
    mode: str,
    allocated: Callable[[], int],
) -> tuple[int, int, int, int, int, int, int, str, str, int | None, int | None, int]:
    stdout_path = log_dir / "sandbox.stdout.log"
    stderr_path = log_dir / "sandbox.stderr.log"
    console_path = log_dir / "guest-console.log"
    stats_path = log_dir / "stats.json"
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
        str(run_root),
        "-base-root",
        str(sandbox_base_root),
        "-ch-binary",
        str(BIN / "cloud-hypervisor"),
        "-console",
        "file=" + str(console_path),
        "-stats-interval",
        "0",
        "-stats-json",
        str(stats_path),
        "-ping-fatal-threshold",
        "0",
    ]
    (log_dir / "sandbox-command.json").write_text(
        json.dumps(command, indent=2) + "\n", encoding="utf-8"
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    events: queue.Queue[tuple[int, bytes]] = queue.Queue()

    def reader(source, destination: Path) -> None:
        with destination.open("wb") as output:
            while True:
                line = source.readline()
                if not line:
                    return
                timestamp = now_ns()
                output.write(line)
                output.flush()
                events.put((timestamp, line))

    stdout_thread = threading.Thread(target=reader, args=(process.stdout, stdout_path), daemon=True)
    stderr_thread = threading.Thread(target=reader, args=(process.stderr, stderr_path), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    prepare_ns = t0 if mode == MODE_BLK else None
    prepare_allocated = 0 if mode == MODE_BLK else None
    guest_ns = None
    app_ns = None
    read_begin_ns = None
    read_end_ns = None
    app_allocated = None
    final_allocated = None
    app_hash = None
    lower_hash = None
    app_hash_pattern = re.compile(rb"^([0-9a-f]{64})[ \t]+/bin/sh\s*$")
    lower_hash_pattern = re.compile(rb"^([0-9a-f]{64})[ \t]+-\s*$")
    deadline = time.monotonic() + VM_TIMEOUT_SECONDS

    try:
        while process.poll() is None or stdout_thread.is_alive() or stderr_thread.is_alive() or not events.empty():
            if process.poll() is None and time.monotonic() > deadline:
                raise TimeoutError(f"sandbox exceeded {VM_TIMEOUT_SECONDS}s")
            try:
                timestamp, line = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if prepare_ns is None and b"lazy root prepared:" in line:
                prepare_ns = timestamp
                prepare_allocated = allocated()
            if guest_ns is None and b"launch: launch_ack received" in line:
                guest_ns = timestamp
            match = app_hash_pattern.match(line.strip())
            if match:
                app_hash = match.group(1).decode()
            match = lower_hash_pattern.match(line.strip())
            if match:
                lower_hash = match.group(1).decode()
            if app_ns is None and b"KUASAR_APP_READY" in line:
                app_ns = timestamp
                app_allocated = allocated()
            if read_begin_ns is None and b"KUASAR_READ_BEGIN" in line:
                read_begin_ns = timestamp
            if read_end_ns is None and b"KUASAR_READ_END" in line:
                read_end_ns = timestamp
                final_allocated = allocated()
        return_code = process.wait(timeout=10)
    except BaseException:
        base.terminate_group(process)
        raise
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    if return_code != 0:
        raise RuntimeError(f"sandbox exited with status {return_code}")
    values = {
        "prepare": prepare_ns,
        "prepare_allocated": prepare_allocated,
        "guest": guest_ns,
        "application": app_ns,
        "read_begin": read_begin_ns,
        "read_end": read_end_ns,
        "application_allocated": app_allocated,
        "final_allocated": final_allocated,
        "application_hash": app_hash,
        "lower_hash": lower_hash,
    }
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise RuntimeError("missing VM observations: " + ", ".join(missing))

    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    if mode == MODE_LAZY:
        if "lazy=on" not in stderr or "sandbox.root.lower=pmem1" not in stderr:
            raise RuntimeError("lazy-pmem command line evidence is missing")
    elif "lazy=on" in stderr or stderr.count("vhost_user=on") < 2:
        raise RuntimeError("vhost-user-blk command line evidence is missing")

    lower_read_count = None
    lower_read_bytes = None
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    if mode == MODE_BLK:
        backends = stats.get("backends", [])
        if len(backends) < 2:
            raise RuntimeError("vhost-user-blk sample did not expose lower and upper backends")
        lower_read_count = int(backends[0]["read"]["count"])
        lower_read_bytes = int(backends[0]["read"]["bytes"])

    return (
        prepare_ns,
        guest_ns,
        app_ns,
        read_begin_ns,
        read_end_ns,
        prepare_allocated,
        app_allocated,
        app_hash,
        lower_hash,
        lower_read_count,
        lower_read_bytes,
    ) + (final_allocated,)


def start_integrated_lazy(
    base,
    runtime_dir: Path,
    socket_dir: Path,
    log_dir: Path,
    manifest_config: Path,
) -> tuple[subprocess.Popen[bytes], Path, Path, Path, Path]:
    cache_root = runtime_dir / "accelerator-lazy-cache"
    range_socket = socket_dir / "integrated-range.sock"
    control_socket = socket_dir / "integrated-control.sock"
    data_socket = socket_dir / "integrated-data.sock"
    cache_root.mkdir(parents=True, exist_ok=False)
    command = [
        str(INTEGRATED_MANIFEST_CTL),
        "lazy-serve",
        "-manifest-config",
        str(manifest_config),
        "-range-socket",
        str(range_socket),
        "-control-socket",
        str(control_socket),
        "-data-socket",
        str(data_socket),
        "-cache-dir",
        str(cache_root),
    ]
    (log_dir / "integrated-command.json").write_text(
        json.dumps(command, indent=2) + "\n", encoding="utf-8"
    )
    log = (log_dir / "integrated-accelerator.log").open("wb")
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    process._benchmark_log = log
    try:
        for socket in (range_socket, control_socket, data_socket):
            base.wait_for_socket(socket, process)
    except BaseException:
        base.close_backend_process(process)
        raise
    return process, cache_root, range_socket, control_socket, data_socket


def run_sample(
    base,
    context,
    phase: str,
    image: Image,
    mode: str,
    round_number: int,
    order: int,
) -> Sample:
    sample_id = f"{image.name}-r{round_number:02d}-o{order}-{mode}"
    runtime_dir = RUNTIME / phase / sample_id
    log_dir = LOGS / phase / sample_id
    socket_dir = base.short_socket_dir(sample_id)
    if runtime_dir.exists() or log_dir.exists() or socket_dir.exists():
        raise RuntimeError(f"sample path already exists: {sample_id}")
    runtime_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    socket_dir.mkdir(parents=True)
    config = log_dir / "sandbox.yaml"
    upper = runtime_dir / "upper.ext4"
    backend = None
    cache_root = runtime_dir / "no-cache"
    try:
        if mode == MODE_LAZY:
            backend, cache_root, range_socket, control_socket, data_socket = start_integrated_lazy(
                base, runtime_dir, socket_dir, log_dir, context.manifest_config
            )
        elif mode == MODE_BLK:
            range_socket = context.range_socket
            control_socket = None
            data_socket = None
        else:
            raise ValueError(f"unknown mode: {mode}")
        write_sandbox_config(
            config,
            mode=mode,
            image=image,
            tap=context.taps[0],
            upper=upper,
            kernel=BIN / "vmlinux",
            runtime=BIN / "sandbox-runtime.erofs",
            upper_template=context.upper_template,
            range_socket=range_socket,
            control_socket=control_socket,
            data_socket=data_socket,
        )
        allocated = (lambda: base.allocated_tree(cache_root)) if mode == MODE_LAZY else (lambda: 0)
        t0 = now_ns()
        observations = wait_vm(
            base=base,
            context=context,
            sample_id=sample_id,
            config=config,
            log_dir=log_dir,
            run_root=socket_dir / "run-root",
            sandbox_base_root=runtime_dir / "base-root",
            t0=t0,
            mode=mode,
            allocated=allocated,
        )
        (
            prepare_ns,
            guest_ns,
            app_ns,
            read_begin_ns,
            read_end_ns,
            prepare_allocated,
            app_allocated,
            app_hash,
            lower_hash,
            lower_read_count,
            lower_read_bytes,
            final_allocated,
        ) = observations
        if mode == MODE_LAZY:
            base.write_tree_stats(cache_root, log_dir / "materialization-final.txt")
        return Sample(
            image=image,
            mode=mode,
            round_number=round_number,
            execution_order=order,
            prepare_seconds=seconds(t0, prepare_ns),
            guest_ready_seconds=seconds(t0, guest_ns),
            application_ready_seconds=seconds(t0, app_ns),
            sequential_read_seconds=seconds(read_begin_ns, read_end_ns),
            end_to_end_seconds=seconds(t0, read_end_ns),
            prepare_allocated_bytes=prepare_allocated,
            application_ready_allocated_bytes=app_allocated,
            final_allocated_bytes=final_allocated,
            application_hash=app_hash,
            lower_hash=lower_hash,
            lower_read_count=lower_read_count,
            lower_read_bytes=lower_read_bytes,
            log_path=str(log_dir.relative_to(ROOT)),
        )
    finally:
        base.close_backend_process(backend)
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        if socket_dir.exists():
            shutil.rmtree(socket_dir)


FIELDS = [
    "image",
    "resolved_digest",
    "manifest_key",
    "mode",
    "round",
    "execution_order",
    "prepare_seconds",
    "guest_ready_seconds",
    "application_ready_seconds",
    "sequential_read_seconds",
    "end_to_end_seconds",
    "prepare_allocated_bytes",
    "application_ready_allocated_bytes",
    "final_allocated_bytes",
    "application_hash",
    "lower_hash",
    "lower_read_count",
    "lower_read_bytes",
    "result",
    "log_path",
]


def sample_row(sample: Sample) -> dict[str, object]:
    return {
        "image": sample.image.name,
        "resolved_digest": sample.image.resolved_digest,
        "manifest_key": sample.image.manifest_key,
        "mode": sample.mode,
        "round": sample.round_number,
        "execution_order": sample.execution_order,
        "prepare_seconds": f"{sample.prepare_seconds:.9f}",
        "guest_ready_seconds": f"{sample.guest_ready_seconds:.9f}",
        "application_ready_seconds": f"{sample.application_ready_seconds:.9f}",
        "sequential_read_seconds": f"{sample.sequential_read_seconds:.9f}",
        "end_to_end_seconds": f"{sample.end_to_end_seconds:.9f}",
        "prepare_allocated_bytes": sample.prepare_allocated_bytes,
        "application_ready_allocated_bytes": sample.application_ready_allocated_bytes,
        "final_allocated_bytes": sample.final_allocated_bytes,
        "application_hash": sample.application_hash,
        "lower_hash": sample.lower_hash,
        "lower_read_count": "" if sample.lower_read_count is None else sample.lower_read_count,
        "lower_read_bytes": "" if sample.lower_read_bytes is None else sample.lower_read_bytes,
        "result": "success",
        "log_path": sample.log_path,
    }


def load_existing_rows(path: Path) -> dict[tuple[str, int, str], dict[str, str]]:
    rows: dict[tuple[str, int, str], dict[str, str]] = {}
    if not path.exists():
        return rows
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != FIELDS:
            raise RuntimeError(f"unexpected result schema in {path}")
        for row in reader:
            key = (row["image"], int(row["round"]), row["mode"])
            if key in rows:
                raise RuntimeError(f"duplicate result row: {key}")
            rows[key] = row
    return rows


def run_pairs(images: list[Image], rounds: int, output: Path, phase: str, resume: bool) -> None:
    if output.exists() and not resume:
        raise RuntimeError(f"refusing to overwrite {output}")
    if resume and not output.exists():
        raise RuntimeError(f"cannot resume missing result file: {output}")
    base = load_base_runner()
    output.parent.mkdir(parents=True, exist_ok=True)
    existing = load_existing_rows(output) if resume else {}
    with output.open("a" if resume else "x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
        if not resume:
            writer.writeheader()
        stream.flush()
        with base.PhaseContext(phase, 1) as context:
            for image in images:
                for round_number in range(1, rounds + 1):
                    modes = MODES if round_number % 2 else tuple(reversed(MODES))
                    pair_rows: dict[str, dict[str, str]] = {}
                    new_samples: list[Sample] = []
                    for mode in modes:
                        row = existing.get((image.name, round_number, mode))
                        if row is not None:
                            pair_rows[mode] = row
                    if len(pair_rows) == len(MODES):
                        continue
                    for order, mode in enumerate(modes, start=1):
                        if mode in pair_rows:
                            continue
                        sample = run_sample(
                            base, context, phase, image, mode, round_number, order
                        )
                        new_samples.append(sample)
                        pair_rows[mode] = {
                            "application_hash": sample.application_hash,
                            "lower_hash": sample.lower_hash,
                        }
                        print(
                            f"image={image.name} round={round_number}/{rounds} order={order} "
                            f"mode={mode} app={sample.application_ready_seconds:.6f}s "
                            f"read={sample.sequential_read_seconds:.6f}s",
                            flush=True,
                        )
                    if len({row["application_hash"] for row in pair_rows.values()}) != 1:
                        raise RuntimeError(f"paired application hashes differ: {image.name}")
                    if len({row["lower_hash"] for row in pair_rows.values()}) != 1:
                        raise RuntimeError(f"paired lower hashes differ: {image.name}")
                    for sample in new_samples:
                        writer.writerow(sample_row(sample))
                    stream.flush()


def parse_names(value: str) -> set[str] | None:
    names = {item.strip() for item in value.split(",") if item.strip()}
    return names or None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["smoke", "formal"])
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--images", default="")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.rounds <= 0:
        raise RuntimeError("rounds must be positive")
    images = load_images(parse_names(args.images))
    if args.phase == "smoke":
        if args.resume:
            raise RuntimeError("smoke does not support --resume")
        images = [next((image for image in images if image.name == "alpine-3.20.3"), images[0])]
        run_pairs(images, 1, ROOT / "smoke-results.tsv", "smoke", False)
    else:
        run_pairs(images, args.rounds, ROOT / "raw-results.tsv", "formal", args.resume)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"benchmark failed: {error}", file=sys.stderr)
        raise
