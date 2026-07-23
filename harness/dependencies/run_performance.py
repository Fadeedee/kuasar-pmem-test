#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ROOT = Path("/root/virtiolazyd/benchmark-results/20260720-canonical-extents")
BIN = ROOT / "bin"
SCRIPTS = ROOT / "scripts"
RUNTIME = ROOT / "runtime"
LOGS = ROOT / "logs"
STORE_ROOT = RUNTIME / "prep2" / "store-data"
MANIFEST_KEY = "8c78f85071758a901dd9e57bd436f3dfe5dc9f9f792f8245528b9e36e6b85f8a"
BRANCH = "canonical-extents"
ALIGNMENT_BYTES = 2 * 1024 * 1024
FETCH_UNIT_BYTES = 1024 * 1024
VM_TIMEOUT_SECONDS = 300
PREP_TIMEOUT_SECONDS = 900
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


@dataclass
class SampleResult:
    mode: str
    image: Image
    round_number: int
    execution_order: int
    prepare_seconds: float
    guest_ready_seconds: float
    application_ready_seconds: float
    prepare_allocated_bytes: int
    application_ready_allocated_bytes: int
    guest_file_sha256: str
    log_path: str


def now_ns() -> int:
    return time.clock_gettime_ns(RAW_CLOCK)


def seconds(start: int, end: int) -> float:
    return (end - start) / 1_000_000_000


def short_socket_dir(label: str) -> Path:
    identity = f"{label}-{os.getpid()}-{now_ns()}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:12]
    return Path("/tmp") / f"kpmb-{suffix}"


def load_images() -> list[Image]:
    images: list[Image] = []
    with (ROOT / "images.tsv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
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
    if len(images) != 5:
        raise RuntimeError(f"expected 5 images, found {len(images)}")
    return images


def source_commit_string() -> str:
    entries: list[str] = []
    with (ROOT / "source-state-start.tsv").open(newline="", encoding="utf-8") as stream:
        rows = csv.DictReader((line for line in stream if not line.startswith("captured_at")), delimiter="\t")
        for row in rows:
            entries.append(f"{Path(row['path']).name}={row['head']}")
    return ";".join(entries)


def allocated_file(path: Path) -> int:
    return path.stat().st_blocks * 512


def allocated_tree(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += allocated_file(entry)
        except FileNotFoundError:
            continue
    return total


def write_tree_stats(root: Path, output: Path) -> None:
    with output.open("w", encoding="utf-8") as stream:
        if not root.exists():
            stream.write("missing\n")
            return
        for path in sorted(root.rglob("*")):
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if path.is_file():
                stream.write(
                    f"{path.relative_to(root)} size={info.st_size} blocks={info.st_blocks} "
                    f"allocated={info.st_blocks * 512} mtime_ns={info.st_mtime_ns}\n"
                )


def terminate_group(process: subprocess.Popen[bytes] | None, timeout: float = 10.0) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=timeout)


def wait_for_socket(path: Path, process: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process {process.pid} exited before socket {path}")
        try:
            if stat.S_ISSOCK(path.stat().st_mode):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"socket did not appear: {path}")


def run_logged(command: list[str], log: Path, timeout: int, env: dict[str, str] | None = None) -> None:
    with log.open("ab") as stream:
        stream.write(("COMMAND " + json.dumps(command) + "\n").encode())
        completed = subprocess.run(
            command,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"command failed ({completed.returncode}): {command}")


class PhaseContext:
    def __init__(self, phase: str, tap_count: int):
        self.phase = phase
        self.tap_count = tap_count
        self.run_id = f"{phase}-{now_ns()}"
        self.runtime_dir = short_socket_dir("services-" + phase)
        self.log_dir = LOGS / "services" / self.run_id
        self.store_socket = self.runtime_dir / "store.sock"
        self.range_socket = self.runtime_dir / "manifest-range.sock"
        self.store_config = self.log_dir / "store.yaml"
        self.manifest_config = self.log_dir / "manifest.yaml"
        self.store_process: subprocess.Popen[bytes] | None = None
        self.range_process: subprocess.Popen[bytes] | None = None
        self.log_streams: list[object] = []
        self.taps: list[str] = []

    def __enter__(self) -> "PhaseContext":
        self.runtime_dir.mkdir(parents=True, exist_ok=False)
        self.log_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._write_configs()
            self._ensure_upper_template()
            self._create_taps()
            self._start_services()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        terminate_group(self.range_process)
        terminate_group(self.store_process)
        for stream in self.log_streams:
            stream.close()
        for tap in reversed(self.taps):
            subprocess.run(["ip", "link", "delete", tap], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    @property
    def upper_template(self) -> Path:
        return RUNTIME / "common" / "upper-template-256m.ext4"

    def _write_configs(self) -> None:
        self.store_config.write_text(
            "\n".join(
                [
                    f"listen: {self.store_socket}",
                    "backend: fs",
                    "stats_interval: 0",
                    "fs:",
                    f"  root: {STORE_ROOT}",
                    "  verify_content_key: true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.manifest_config.write_text(
            "\n".join(
                [
                    "manifest:",
                    f'  key: "{MANIFEST_KEY}"',
                    "store:",
                    f"  endpoint: {self.store_socket}",
                    "  pool: 4",
                    "  timeout: 30s",
                    "cache:",
                    '  endpoint: ""',
                    "  pool: 1",
                    "  timeout: 30s",
                    "chunker:",
                    "  mode: cdc",
                    "  cdc:",
                    "    min: 128KiB",
                    "    avg: 512KiB",
                    "    max: 1MiB",
                    "  fixed:",
                    "    size: 512KiB",
                    "crypto:",
                    "  chunk: aes",
                    "  manifest: aes",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _ensure_upper_template(self) -> None:
        path = self.upper_template
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.stat().st_size != 256 * 1024 * 1024:
                raise RuntimeError(f"unexpected upper template size: {path}")
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(fd, 256 * 1024 * 1024)
        finally:
            os.close(fd)
        run_logged(
            ["mkfs.ext4", "-q", "-F", str(path)],
            self.log_dir / "upper-template.log",
            timeout=120,
        )

    def _create_taps(self) -> None:
        for index in range(self.tap_count):
            tap = f"kpmb{index}"
            exists = subprocess.run(
                ["ip", "link", "show", tap],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if exists:
                raise RuntimeError(f"refusing to reuse existing TAP: {tap}")
            subprocess.run(["ip", "tuntap", "add", "dev", tap, "mode", "tap"], check=True)
            subprocess.run(["ip", "link", "set", tap, "up"], check=True)
            self.taps.append(tap)

    def _start_services(self) -> None:
        store_log = (self.log_dir / "store.log").open("wb")
        self.log_streams.append(store_log)
        self.store_process = subprocess.Popen(
            [str(BIN / "store-ctl"), "serve", "-config", str(self.store_config)],
            stdout=store_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait_for_socket(self.store_socket, self.store_process)

        range_log = (self.log_dir / "manifest-range.log").open("wb")
        self.log_streams.append(range_log)
        self.range_process = subprocess.Popen(
            [
                str(BIN / "manifest-ctl"),
                "serve",
                "-manifest-config",
                str(self.manifest_config),
                "-socket",
                str(self.range_socket),
            ],
            stdout=range_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait_for_socket(self.range_socket, self.range_process)


def start_lazyd(
    runtime_dir: Path, socket_dir: Path, log_dir: Path, trace_path: Path | None
) -> tuple[subprocess.Popen[bytes], Path, Path, Path]:
    cache = runtime_dir / "lazyd-cache"
    control_socket = socket_dir / "lazyd-control.sock"
    data_socket = socket_dir / "lazyd-data.sock"
    cache.mkdir(parents=True, exist_ok=False)
    command = [
        "unshare",
        "--mount",
        "--propagation",
        "private",
        "--",
        "sh",
        "-c",
        'cache=$1; shift; mount --bind "$cache" /var/lib/lazyd/images && exec env "$@"',
        "sh",
        str(cache),
    ]
    if trace_path is not None:
        command.extend(
            [
                f"LD_PRELOAD={BIN / 'pmem-smoke-trace.so'}",
                f"KUASAR_PMEM_TRACE={trace_path}",
                "KUASAR_PMEM_TRACE_ROLE=lazyd",
            ]
        )
    command.extend(
        [
            "RUST_LOG=warn",
            str(BIN / "lazyd"),
            "--socket",
            str(control_socket),
            "--data-socket",
            str(data_socket),
            "--disable-fanotify",
        ]
    )
    log = (log_dir / "lazyd.log").open("wb")
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    process._benchmark_log = log  # type: ignore[attr-defined]
    wait_for_socket(control_socket, process)
    wait_for_socket(data_socket, process)
    return process, cache, control_socket, data_socket


def start_fake_lazyd(
    socket_dir: Path, log_dir: Path, image: Image, full_file: Path
) -> tuple[subprocess.Popen[bytes], Path, Path]:
    control_socket = socket_dir / "fake-lazyd-control.sock"
    ready_file = socket_dir / "fake-lazyd.ready"
    data_socket = socket_dir / "unused-lazyd-data.sock"
    command = [
        str(SCRIPTS / "fake-lazyd-control.py"),
        "--socket",
        str(control_socket),
        "--ready-file",
        str(ready_file),
        "--full-file",
        str(full_file),
        "--digest",
        "sha256:" + image.manifest_key,
        "--blob-size",
        str(image.manifest_image_bytes),
        "--pmem-size",
        str(image.pmem_bytes),
    ]
    log = (log_dir / "fake-lazyd.log").open("wb")
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    process._benchmark_log = log  # type: ignore[attr-defined]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("fake lazyd exited before readiness")
        if ready_file.exists() and control_socket.exists():
            return process, control_socket, data_socket
        time.sleep(0.05)
    raise RuntimeError("fake lazyd did not become ready")


def write_sandbox_config(
    path: Path,
    context: PhaseContext,
    image: Image,
    tap: str,
    upper: Path,
    control_socket: Path,
    data_socket: Path,
    launch_script: str = "sha256sum /bin/sh; echo KUASAR_REAL_READY",
) -> None:
    quote = json.dumps
    lines = [
        "resources:",
        "  capacity: { cpu: 1, memory: 512MiB }",
        "  allocatable: { cpu: 1, memory: 512MiB }",
        "network:",
        f"  tap: {quote(tap)}",
        '  ip: ""',
        '  hostname: "pmem-benchmark"',
        "boot:",
        f"  kernel: {quote('file://' + str(BIN / 'vmlinux'))}",
        f"  runtime: {quote('file://' + str(BIN / 'sandbox-runtime.erofs'))}",
        "  root:",
        f"    base: {quote('manifest://' + image.manifest_key)}",
        "    lower_device: lazy-pmem",
        "    overlay:",
        f"      diff: {quote('file://' + str(upper))}",
        f"      diff_template: {quote('file://' + str(context.upper_template))}",
        "    lazy_pmem:",
        f"      lazyd_control_socket: {quote(str(control_socket))}",
        f"      lazyd_data_socket: {quote(str(data_socket))}",
        f"      accelerator_socket: {quote(str(context.range_socket))}",
        f"      fetch_unit_bytes: {FETCH_UNIT_BYTES}",
        f"      alignment_bytes: {ALIGNMENT_BYTES}",
        "launch:",
        '  exec: "/bin/sh"',
        f"  args: {json.dumps(['-c', launch_script])}",
        "  restart: never",
        "  start_timeout: 60s",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_vm(
    context: PhaseContext,
    sample_id: str,
    config: Path,
    log_dir: Path,
    t0: int,
    prepare_ns: int | None,
    allocated: Callable[[], int],
    ch_binary: Path,
    environment: dict[str, str],
    run_root: Path,
    base_root: Path,
) -> tuple[int, int, int, int, int, str]:
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
        str(base_root),
        "-ch-binary",
        str(ch_binary),
        "-console",
        "file=" + str(console_path),
        "-stats-interval",
        "0",
        "-stats-json",
        str(stats_path),
        "-ping-fatal-threshold",
        "0",
    ]
    (log_dir / "sandbox-command.json").write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    events: queue.Queue[tuple[str, int, bytes]] = queue.Queue()

    def reader(name: str, source, destination: Path) -> None:
        with destination.open("wb") as output:
            while True:
                line = source.readline()
                if not line:
                    break
                timestamp = now_ns()
                output.write(line)
                output.flush()
                events.put((name, timestamp, line))

    stdout_thread = threading.Thread(target=reader, args=("stdout", process.stdout, stdout_path), daemon=True)
    stderr_thread = threading.Thread(target=reader, args=("stderr", process.stderr, stderr_path), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    guest_ready_ns: int | None = None
    application_ready_ns: int | None = None
    prepare_allocated: int | None = allocated() if prepare_ns is not None else None
    application_allocated: int | None = None
    guest_hash: str | None = None
    deadline = time.monotonic() + VM_TIMEOUT_SECONDS
    hash_pattern = re.compile(rb"^([0-9a-f]{64})[ \t]+/bin/sh\s*$")

    try:
        while process.poll() is None or stdout_thread.is_alive() or stderr_thread.is_alive() or not events.empty():
            if time.monotonic() > deadline and process.poll() is None:
                raise TimeoutError(f"sandbox exceeded {VM_TIMEOUT_SECONDS}s")
            try:
                stream_name, timestamp, line = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if prepare_ns is None and b"lazy root prepared:" in line:
                prepare_ns = timestamp
                prepare_allocated = allocated()
            if guest_ready_ns is None and b"launch: launch_ack received" in line:
                guest_ready_ns = timestamp
            match = hash_pattern.match(line.strip())
            if match:
                guest_hash = match.group(1).decode()
            if application_ready_ns is None and b"KUASAR_REAL_READY" in line:
                application_ready_ns = timestamp
                application_allocated = allocated()
        return_code = process.wait(timeout=10)
    except BaseException:
        terminate_group(process)
        raise
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    if return_code != 0:
        raise RuntimeError(f"sandbox exited with status {return_code}")
    missing = [
        name
        for name, value in (
            ("prepare", prepare_ns),
            ("guest_ready", guest_ready_ns),
            ("application_ready", application_ready_ns),
            ("prepare_allocated", prepare_allocated),
            ("application_allocated", application_allocated),
            ("guest_hash", guest_hash),
        )
        if value is None
    ]
    if missing:
        raise RuntimeError("missing VM observations: " + ", ".join(missing))
    console = console_path.read_text(encoding="utf-8", errors="replace")
    if "DAX enabled" not in console:
        raise RuntimeError("guest console does not contain EROFS DAX evidence")
    return prepare_ns, guest_ready_ns, application_ready_ns, prepare_allocated, application_allocated, guest_hash  # type: ignore[return-value]


def close_backend_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    terminate_group(process)
    log = getattr(process, "_benchmark_log", None)
    if log is not None:
        log.close()


def run_sample(
    context: PhaseContext,
    phase: str,
    image: Image,
    mode: str,
    round_number: int,
    order: int,
    trace_path: Path | None = None,
) -> SampleResult:
    sample_base = f"{image.name}-r{round_number:02d}-o{order}-{mode}"
    sample_name = sample_base
    attempt = 1
    while (RUNTIME / phase / sample_name).exists() or (LOGS / phase / sample_name).exists():
        attempt += 1
        sample_name = f"{sample_base}-a{attempt}"
    runtime_dir = RUNTIME / phase / sample_name
    log_dir = LOGS / phase / sample_name
    socket_dir = short_socket_dir(sample_name)
    runtime_dir.mkdir(parents=True, exist_ok=False)
    log_dir.mkdir(parents=True, exist_ok=False)
    socket_dir.mkdir(parents=True, exist_ok=False)
    config = log_dir / "sandbox.yaml"
    upper = runtime_dir / "upper.ext4"
    backend_process: subprocess.Popen[bytes] | None = None
    try:
        if mode == "lazy":
            backend_process, materialized_root, control_socket, data_socket = start_lazyd(
                runtime_dir, socket_dir, log_dir, trace_path
            )
            write_sandbox_config(config, context, image, context.taps[0], upper, control_socket, data_socket)
            environment = os.environ.copy()
            if trace_path is not None:
                environment.update(
                    {
                        "LD_PRELOAD": str(BIN / "pmem-smoke-trace.so"),
                        "KUASAR_PMEM_TRACE": str(trace_path),
                        "KUASAR_PMEM_TRACE_ROLE": "cloud-hypervisor",
                    }
                )
            t0 = now_ns()
            observations = run_vm(
                context,
                sample_name,
                config,
                log_dir,
                t0,
                None,
                lambda: allocated_tree(materialized_root),
                BIN / "cloud-hypervisor",
                environment,
                socket_dir / "run-root",
                runtime_dir / "base-root",
            )
            prepare_ns, guest_ns, app_ns, prepare_allocated, app_allocated, guest_hash = observations
            write_tree_stats(materialized_root, log_dir / "materialization-final.txt")
        elif mode == "full":
            full_tar = runtime_dir / "full-pmem.tar"
            full_file = runtime_dir / "full-pmem.erofs"
            backend_process, control_socket, data_socket = start_fake_lazyd(
                socket_dir, log_dir, image, full_file
            )
            write_sandbox_config(config, context, image, context.taps[0], upper, control_socket, data_socket)
            prep_log = log_dir / "full-prepare.log"
            t0 = now_ns()
            run_logged(
                [
                    str(BIN / "manifest-ctl"),
                    "load",
                    "-manifest-config",
                    str(context.manifest_config),
                    "-no-progress",
                    "-output",
                    str(full_tar),
                    "manifest://" + image.manifest_key,
                ],
                prep_log,
                PREP_TIMEOUT_SECONDS,
            )
            run_logged(
                [
                    str(BIN / "flatten-ctl"),
                    "tar",
                    "extract",
                    "-f",
                    str(full_tar),
                    "image:" + str(full_file),
                ],
                prep_log,
                PREP_TIMEOUT_SECONDS,
            )
            os.truncate(full_file, image.pmem_bytes)
            prepare_ns = now_ns()
            prepare_allocated = allocated_file(full_file)
            (log_dir / "materialization-prepare.txt").write_text(
                f"size={full_file.stat().st_size} blocks={full_file.stat().st_blocks} "
                f"allocated={prepare_allocated}\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "REAL_CLOUD_HYPERVISOR": str(BIN / "cloud-hypervisor"),
                    "FULL_PMEM_FILE": str(full_file),
                    "FULL_PMEM_SIZE": str(image.pmem_bytes),
                    "FULL_WRAPPER_LOG": str(log_dir / "full-wrapper-command.jsonl"),
                }
            )
            observations = run_vm(
                context,
                sample_name,
                config,
                log_dir,
                t0,
                prepare_ns,
                lambda: allocated_file(full_file),
                SCRIPTS / "full-pmem-ch-wrapper.py",
                environment,
                socket_dir / "run-root",
                runtime_dir / "base-root",
            )
            _, guest_ns, app_ns, _, app_allocated, guest_hash = observations
        else:
            raise ValueError(f"unknown mode: {mode}")

        return SampleResult(
            mode=mode,
            image=image,
            round_number=round_number,
            execution_order=order,
            prepare_seconds=seconds(t0, prepare_ns),
            guest_ready_seconds=seconds(t0, guest_ns),
            application_ready_seconds=seconds(t0, app_ns),
            prepare_allocated_bytes=prepare_allocated,
            application_ready_allocated_bytes=app_allocated,
            guest_file_sha256=guest_hash,
            log_path=str(log_dir.relative_to(ROOT)),
        )
    finally:
        close_backend_process(backend_process)
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        if socket_dir.exists():
            shutil.rmtree(socket_dir)


RAW_FIELDS = [
    "branch",
    "commit",
    "image",
    "digest",
    "manifest_key",
    "mode",
    "round",
    "execution_order",
    "prepare_seconds",
    "guest_ready_seconds",
    "application_ready_seconds",
    "prepare_allocated_bytes",
    "application_ready_allocated_bytes",
    "guest_file_sha256",
    "uffd_fault_count",
    "fetch_count",
    "accelerator_read_range_count",
    "result",
    "log_path",
]


def result_row(result: SampleResult, commits: str) -> dict[str, object]:
    return {
        "branch": BRANCH,
        "commit": commits,
        "image": result.image.name,
        "digest": result.image.resolved_digest,
        "manifest_key": result.image.manifest_key,
        "mode": result.mode,
        "round": result.round_number,
        "execution_order": result.execution_order,
        "prepare_seconds": f"{result.prepare_seconds:.9f}",
        "guest_ready_seconds": f"{result.guest_ready_seconds:.9f}",
        "application_ready_seconds": f"{result.application_ready_seconds:.9f}",
        "prepare_allocated_bytes": result.prepare_allocated_bytes,
        "application_ready_allocated_bytes": result.application_ready_allocated_bytes,
        "guest_file_sha256": result.guest_file_sha256,
        "uffd_fault_count": "unavailable",
        "fetch_count": "unavailable",
        "accelerator_read_range_count": "unavailable",
        "result": "success",
        "log_path": result.log_path,
    }


def write_failure(path: Path, phase: str, image: str, mode: str, round_number: int, stage: str, error: BaseException, log_path: str) -> None:
    create = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        if create:
            writer.writerow(["phase", "image", "mode", "round", "stage", "error", "log_path"])
        writer.writerow([phase, image, mode, round_number, stage, str(error), log_path])


def run_smoke(images: list[Image]) -> None:
    output = ROOT / "smoke-results.tsv"
    if output.exists():
        raise RuntimeError(f"refusing to overwrite smoke results: {output}")
    image = next(item for item in images if item.name == "alpine-3.20.3")
    trace = LOGS / "smoke" / "lazy-smoke-trace.log"
    trace.parent.mkdir(parents=True, exist_ok=True)
    with PhaseContext("smoke", 1) as context:
        lazy = run_sample(context, "smoke", image, "lazy", 0, 1, trace)
        full = run_sample(context, "smoke", image, "full", 0, 2)
    if lazy.guest_file_sha256 != full.guest_file_sha256:
        raise RuntimeError("smoke Full/Lazy /bin/sh hashes differ")
    evidence = trace.read_text(encoding="utf-8", errors="replace")
    required = [
        "event=UFFDIO_REGISTER",
        "event=FETCH_SEND",
        "event=READ_RANGE_SEND",
        "event=SEND_FD",
        "event=RECV_FD",
        "event=MAP_FIXED",
        "event=UFFDIO_WAKE",
    ]
    missing = [item for item in required if item not in evidence]
    if missing:
        raise RuntimeError("smoke trace missing: " + ", ".join(missing))
    commits = source_commit_string()
    with output.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=RAW_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerow(result_row(lazy, commits))
        writer.writerow(result_row(full, commits))
    counts = {event: evidence.count(event) for event in required}
    (ROOT / "smoke-evidence.json").write_text(
        json.dumps(
            {
                "image": image.name,
                "guest_file_sha256": lazy.guest_file_sha256,
                "trace_counts": counts,
                "trace_path": str(trace.relative_to(ROOT)),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def run_formal(images: list[Image], rounds: int, resume: bool) -> None:
    output = ROOT / "raw-results.tsv"
    failures = ROOT / "failures.tsv"
    commits = source_commit_string()
    existing: dict[tuple[str, int, str], dict[str, str]] = {}
    if resume:
        if not output.exists() or not failures.exists():
            raise RuntimeError("--resume requires existing raw-results.tsv and failures.tsv")
        with output.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream, delimiter="\t"):
                key = (row["image"], int(row["round"]), row["mode"])
                if key in existing:
                    raise RuntimeError(f"duplicate existing formal result: {key}")
                existing[key] = row
        output_mode = "a"
    else:
        if output.exists() or failures.exists():
            raise RuntimeError("refusing to overwrite existing formal raw results or failures")
        with failures.open("x", newline="", encoding="utf-8") as stream:
            csv.writer(stream, delimiter="\t", lineterminator="\n").writerow(
                ["phase", "image", "mode", "round", "stage", "error", "log_path"]
            )
        output_mode = "x"
    with output.open(output_mode, newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=RAW_FIELDS, delimiter="\t", lineterminator="\n")
        if not resume:
            writer.writeheader()
        stream.flush()
        with PhaseContext("formal", 1) as context:
            for image in images:
                for round_number in range(1, rounds + 1):
                    modes = ["lazy", "full"] if round_number % 2 else ["full", "lazy"]
                    pair_rows = {
                        mode: existing[(image.name, round_number, mode)]
                        for mode in modes
                        if (image.name, round_number, mode) in existing
                    }
                    if len(pair_rows) == 2:
                        continue
                    new_results: list[SampleResult] = []
                    for order, mode in enumerate(modes, start=1):
                        if mode in pair_rows:
                            continue
                        log_path = f"logs/formal/{image.name}-r{round_number:02d}-o{order}-{mode}"
                        try:
                            result = run_sample(
                                context,
                                "formal",
                                image,
                                mode,
                                round_number,
                                order,
                            )
                            new_results.append(result)
                            pair_rows[mode] = {
                                "guest_file_sha256": result.guest_file_sha256,
                                "log_path": result.log_path,
                            }
                            print(
                                f"formal image={image.name} round={round_number}/{rounds} "
                                f"order={order} mode={mode} app={result.application_ready_seconds:.6f}s",
                                flush=True,
                            )
                        except BaseException as error:
                            candidates = sorted(
                                (LOGS / "formal").glob(
                                    f"{image.name}-r{round_number:02d}-o{order}-{mode}*"
                                ),
                                key=lambda path: path.stat().st_mtime_ns,
                            )
                            if candidates:
                                log_path = str(candidates[-1].relative_to(ROOT))
                            write_failure(
                                failures,
                                "formal",
                                image.name,
                                mode,
                                round_number,
                                "sample",
                                error,
                                log_path,
                            )
                            raise
                    if pair_rows["lazy"]["guest_file_sha256"] != pair_rows["full"]["guest_file_sha256"]:
                        error = RuntimeError("paired Full/Lazy /bin/sh hashes differ")
                        write_failure(
                            failures,
                            "formal",
                            image.name,
                            "pair",
                            round_number,
                            "hash-compare",
                            error,
                            pair_rows["lazy"]["log_path"] + ";" + pair_rows["full"]["log_path"],
                        )
                        raise error
                    for result in new_results:
                        writer.writerow(result_row(result, commits))
                    stream.flush()


class PersistentVM:
    def __init__(
        self,
        context: PhaseContext,
        vm_index: int,
        config: Path,
        log_dir: Path,
        runtime_dir: Path,
        socket_dir: Path,
    ):
        self.vm_index = vm_index
        self.log_dir = log_dir
        self.socket_dir = socket_dir
        self.console_path = log_dir / "guest-console.log"
        command = [
            str(BIN / "sandbox-ctl"),
            "run",
            "-config",
            str(config),
            "-manifest-config",
            str(context.manifest_config),
            "-sandbox-id",
            f"sharing-{vm_index}",
            "-run-root",
            str(socket_dir / "run-root"),
            "-base-root",
            str(runtime_dir / "base-root"),
            "-ch-binary",
            str(BIN / "cloud-hypervisor"),
            "-console",
            "file=" + str(self.console_path),
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
        )
        self.ready = threading.Event()
        self.guest_hash: str | None = None
        self.ch_pid: int | None = None
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
        hash_pattern = re.compile(rb"^([0-9a-f]{64})[ \t]+/bin/sh\s*$")
        pid_pattern = re.compile(rb"CH started pid=([0-9]+)")
        with destination.open("wb") as output:
            while True:
                line = source.readline()
                if not line:
                    return
                output.write(line)
                output.flush()
                hash_match = hash_pattern.match(line.strip())
                if hash_match:
                    self.guest_hash = hash_match.group(1).decode()
                pid_match = pid_pattern.search(line)
                if pid_match:
                    self.ch_pid = int(pid_match.group(1))
                if b"KUASAR_SHARING_READY" in line:
                    self.ready.set()

    def wait_ready(self, timeout: int = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"sharing VM {self.vm_index} exited before ready: {self.process.returncode}"
                )
            if self.ready.wait(timeout=0.1):
                break
        else:
            raise TimeoutError(f"sharing VM {self.vm_index} did not become ready")
        if self.guest_hash is None or self.ch_pid is None:
            raise RuntimeError(f"sharing VM {self.vm_index} missed hash or CH pid")
        console_deadline = time.monotonic() + 5
        while time.monotonic() < console_deadline:
            try:
                if "DAX enabled" in self.console_path.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    return
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        raise RuntimeError(f"sharing VM {self.vm_index} has no EROFS DAX evidence")

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGTERM)
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                terminate_group(self.process)
        for thread in self.threads:
            thread.join(timeout=5)
        if self.socket_dir.exists():
            shutil.rmtree(self.socket_dir)


SMAPS_HEADER = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+) ([rwxps-]{4}) ([0-9a-f]+) "
    r"([0-9a-f]+:[0-9a-f]+) ([0-9]+)\s*(.*)$"
)


def selected_smaps(pid: int, vm_index: int, group_vms: int) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    with Path(f"/proc/{pid}/smaps").open(encoding="utf-8") as stream:
        for raw in stream:
            line = raw.rstrip("\n")
            header = SMAPS_HEADER.match(line)
            if header:
                if current is not None and str(current["path"]).endswith("layer.erofs"):
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
        if current is not None and str(current["path"]).endswith("layer.erofs"):
            sections.append(current)
    if not sections:
        raise RuntimeError(f"no layer.erofs mappings found in /proc/{pid}/smaps")
    return sections


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def cache_snapshot(cache_root: Path, group_vms: int, hashes: list[str]) -> dict[str, object]:
    layers = list(cache_root.rglob("layer.erofs"))
    bitmaps = list(cache_root.rglob("layer.erofs.bitmap"))
    if len(layers) != 1 or len(bitmaps) != 1:
        raise RuntimeError(
            f"expected one shared cache and bitmap, got layers={len(layers)} bitmaps={len(bitmaps)}"
        )
    layer = layers[0]
    bitmap = bitmaps[0]
    layer_stat = layer.stat()
    bitmap_stat = bitmap.stat()
    return {
        "group_vms": group_vms,
        "layer_path": str(layer),
        "layer_dev": f"{os.major(layer_stat.st_dev):02x}:{os.minor(layer_stat.st_dev):02x}",
        "layer_inode": layer_stat.st_ino,
        "layer_size": layer_stat.st_size,
        "layer_blocks": layer_stat.st_blocks,
        "layer_mtime_ns": layer_stat.st_mtime_ns,
        "bitmap_path": str(bitmap),
        "bitmap_blocks": bitmap_stat.st_blocks,
        "bitmap_mtime_ns": bitmap_stat.st_mtime_ns,
        "bitmap_sha256": hash_file(bitmap),
        "total_allocated_bytes": allocated_tree(cache_root),
        "guest_hashes": ",".join(hashes),
        "uffd_fault_count": "unavailable",
        "fetch_count": "unavailable",
        "accelerator_read_range_count": "unavailable",
    }


def wait_cache_stable(cache_root: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    previous: tuple[tuple[str, int, int, int], ...] | None = None
    unchanged = 0
    while time.monotonic() < deadline:
        state = tuple(
            sorted(
                (
                    str(path.relative_to(cache_root)),
                    path.stat().st_size,
                    path.stat().st_blocks,
                    path.stat().st_mtime_ns,
                )
                for path in cache_root.rglob("*")
                if path.is_file()
            )
        )
        if state and state == previous:
            unchanged += 1
            if unchanged >= 4:
                return
        else:
            unchanged = 0
            previous = state
        time.sleep(0.25)
    raise TimeoutError("shared lazy cache did not become stable")


def run_sharing(images: list[Image]) -> None:
    output_dir = ROOT / "sharing"
    cache_output = output_dir / "cache.tsv"
    smaps_output = output_dir / "smaps.tsv"
    result_output = output_dir / "validation.json"
    if cache_output.exists() or smaps_output.exists() or result_output.exists():
        raise RuntimeError("refusing to overwrite existing sharing results")
    image = next(item for item in images if item.name == "openeuler-24.03-lts")
    run_id = f"sharing-{now_ns()}"
    shared_runtime = RUNTIME / "sharing" / run_id
    shared_log = LOGS / "sharing" / run_id
    shared_socket = short_socket_dir(run_id)
    shared_runtime.mkdir(parents=True, exist_ok=False)
    shared_log.mkdir(parents=True, exist_ok=False)
    shared_socket.mkdir(parents=True, exist_ok=False)
    lazyd_process: subprocess.Popen[bytes] | None = None
    vms: list[PersistentVM] = []
    cache_rows: list[dict[str, object]] = []
    try:
        with PhaseContext("sharing", 4) as context:
            lazyd_process, cache_root, control_socket, data_socket = start_lazyd(
                shared_runtime, shared_socket, shared_log, None
            )
            with cache_output.open("x", newline="", encoding="utf-8") as cache_stream, smaps_output.open(
                "x", newline="", encoding="utf-8"
            ) as smaps_stream:
                cache_fields = [
                    "group_vms",
                    "layer_path",
                    "layer_dev",
                    "layer_inode",
                    "layer_size",
                    "layer_blocks",
                    "layer_mtime_ns",
                    "bitmap_path",
                    "bitmap_blocks",
                    "bitmap_mtime_ns",
                    "bitmap_sha256",
                    "total_allocated_bytes",
                    "guest_hashes",
                    "uffd_fault_count",
                    "fetch_count",
                    "accelerator_read_range_count",
                ]
                smaps_fields = [
                    "group_vms",
                    "vm_index",
                    "ch_pid",
                    "start",
                    "end",
                    "perms",
                    "offset",
                    "dev",
                    "inode",
                    "path",
                    "rss_kib",
                    "pss_kib",
                    "shared_clean_kib",
                    "private_clean_kib",
                    "private_dirty_kib",
                ]
                cache_writer = csv.DictWriter(
                    cache_stream, fieldnames=cache_fields, delimiter="\t", lineterminator="\n"
                )
                smaps_writer = csv.DictWriter(
                    smaps_stream, fieldnames=smaps_fields, delimiter="\t", lineterminator="\n"
                )
                cache_writer.writeheader()
                smaps_writer.writeheader()
                for vm_index in range(1, 5):
                    vm_runtime = shared_runtime / f"vm-{vm_index}"
                    vm_log = shared_log / f"vm-{vm_index}"
                    vm_socket = short_socket_dir(f"sharing-vm-{vm_index}")
                    vm_runtime.mkdir(parents=True, exist_ok=False)
                    vm_log.mkdir(parents=True, exist_ok=False)
                    vm_socket.mkdir(parents=True, exist_ok=False)
                    config = vm_log / "sandbox.yaml"
                    write_sandbox_config(
                        config,
                        context,
                        image,
                        context.taps[vm_index - 1],
                        vm_runtime / "upper.ext4",
                        control_socket,
                        data_socket,
                        "sha256sum /bin/sh; echo KUASAR_SHARING_READY; sleep 600",
                    )
                    vm = PersistentVM(
                        context, vm_index, config, vm_log, vm_runtime, vm_socket
                    )
                    vms.append(vm)
                    vm.wait_ready()
                    wait_cache_stable(cache_root)
                    print(
                        f"sharing vm={vm_index} pid={vm.ch_pid} hash={vm.guest_hash}",
                        flush=True,
                    )
                    if vm_index not in (1, 2, 4):
                        continue
                    hashes = [active.guest_hash for active in vms]
                    if len(set(hashes)) != 1:
                        raise RuntimeError(f"sharing guest hashes differ at {vm_index} VMs")
                    cache_row = cache_snapshot(
                        cache_root, vm_index, [value for value in hashes if value is not None]
                    )
                    cache_rows.append(cache_row)
                    cache_writer.writerow(cache_row)
                    cache_stream.flush()
                    group_sections: list[list[dict[str, object]]] = []
                    for active in vms:
                        sections = selected_smaps(active.ch_pid, active.vm_index, vm_index)  # type: ignore[arg-type]
                        group_sections.append(sections)
                        smaps_writer.writerows(sections)
                    smaps_stream.flush()
                    file_ids = [
                        {(row["dev"], row["inode"]) for row in sections}
                        for sections in group_sections
                    ]
                    if any(len(identity) != 1 for identity in file_ids) or any(
                        identity != file_ids[0] for identity in file_ids[1:]
                    ):
                        raise RuntimeError(f"sharing mappings use different files at {vm_index} VMs")
                    signatures = [
                        {
                            (
                                row["offset"],
                                int(str(row["end"]), 16) - int(str(row["start"]), 16),
                            )
                            for row in sections
                        }
                        for sections in group_sections
                    ]
                    common_signatures = set.intersection(*signatures)
                    if not common_signatures:
                        raise RuntimeError(
                            f"sharing mappings have no common file offset/length at {vm_index} VMs"
                        )
                    selected_sections = [
                        [
                            row
                            for row in sections
                            if (
                                row["offset"],
                                int(str(row["end"]), 16) - int(str(row["start"]), 16),
                            )
                            in common_signatures
                        ]
                        for sections in group_sections
                    ]
                    if any(
                        int(row["private_dirty_kib"]) != 0
                        for sections in selected_sections
                        for row in sections
                    ):
                        raise RuntimeError(f"sharing mappings contain Private_Dirty at {vm_index} VMs")
                    if vm_index > 1 and not any(
                        int(row["shared_clean_kib"]) > 0
                        for sections in selected_sections
                        for row in sections
                    ):
                        raise RuntimeError(f"sharing mappings contain no Shared_Clean at {vm_index} VMs")
            baseline = cache_rows[0]
            for row in cache_rows[1:]:
                for field in (
                    "layer_blocks",
                    "layer_mtime_ns",
                    "bitmap_blocks",
                    "bitmap_mtime_ns",
                    "bitmap_sha256",
                ):
                    if row[field] != baseline[field]:
                        raise RuntimeError(f"shared cache field changed: {field}")
            result_output.write_text(
                json.dumps(
                    {
                        "image": image.name,
                        "manifest_key": image.manifest_key,
                        "guest_file_sha256": vms[0].guest_hash,
                        "same_cache_dev_inode": True,
                        "common_offset_length_mappings_observed": True,
                        "private_dirty_zero": True,
                        "shared_clean_observed": True,
                        "cache_blocks_and_mtime_stable": True,
                        "counters": "unavailable: product branches do not export success-path lazy-pmem counters",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    finally:
        for vm in reversed(vms):
            vm.stop()
        close_backend_process(lazyd_process)
        if shared_socket.exists():
            shutil.rmtree(shared_socket)
        if shared_runtime.exists():
            shutil.rmtree(shared_runtime)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["smoke", "formal", "sharing"])
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    images = load_images()
    if args.phase == "smoke":
        run_smoke(images)
    elif args.phase == "formal":
        if args.rounds <= 0:
            raise RuntimeError("rounds must be positive")
        run_formal(images, args.rounds, args.resume)
    else:
        run_sharing(images)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"benchmark failed: {error}", file=sys.stderr)
        raise
