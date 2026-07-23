#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

from benchmark_metrics import (
    CgroupAccountingTracker,
    CgroupMemoryTracker,
    current_cgroup_path,
    parse_counter_summaries,
    reset_memory_peak,
    subtract_counters,
    sum_counters,
)


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_SCRIPT = SCRIPT_DIR / "run_reuse_benchmark.py"
CAPTURE_ORDER = CgroupMemoryTracker.CHECKPOINTS
SOURCE_STATES = ("plaintext-cold", "plaintext-warm")
CACHE_BACKINGS = ("file", "memfd")
WORKLOADS = ("nginx-first-request", "full-tree-scan", "mysql-capability-smoke")
SHA256 = re.compile(r"[0-9a-f]{64}")
ACCELERATOR_COUNTER_MARKER = "KUASAR_BENCH_ACCELERATOR_STATS"
LAZYD_COUNTER_MARKER = "KUASAR_BENCH_LAZYD_STATS"
CH_COUNTER_MARKER = "KUASAR_BENCH_CH_STATS"


def measured_counter_summary(
    path: Path,
    marker: str,
    baseline: dict[str, int] | None,
) -> dict[str, dict[str, int]]:
    summaries = parse_counter_summaries(path.read_text(encoding="utf-8", errors="replace"), marker)
    if not summaries:
        raise RuntimeError(f"missing {marker} in {path}")
    final = summaries[-1]
    baseline = {name: 0 for name in final} if baseline is None else baseline
    return {
        "baseline": baseline,
        "final": final,
        "measured": subtract_counters(final, baseline),
    }


def aggregate_vhost_root_stats(paths: list[Path]) -> dict[str, int]:
    totals = {
        "backend_count": 0,
        "read_requests": 0,
        "read_bytes": 0,
        "read_errors": 0,
        "loaded_blocks": 0,
        "total_blocks": 0,
    }
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        roots = [backend for backend in report.get("backends", []) if backend.get("name") == "blk0"]
        if len(roots) > 1:
            raise RuntimeError(f"multiple blk0 backends in {path}")
        if not roots:
            continue
        root = roots[0]
        read = root.get("read", {})
        totals["backend_count"] += 1
        totals["read_requests"] += int(read.get("count", 0))
        totals["read_bytes"] += int(read.get("bytes", 0))
        totals["read_errors"] += int(read.get("err_count", 0))
        totals["loaded_blocks"] += int(root.get("loaded_blocks", 0))
        totals["total_blocks"] += int(root.get("total_blocks", 0))
    return totals


def request_counter_snapshot(
    process,
    path: Path,
    marker: str,
    *,
    timeout: float = 5.0,
) -> dict[str, int]:
    before = (
        parse_counter_summaries(path.read_text(encoding="utf-8", errors="replace"), marker)
        if path.exists()
        else []
    )
    if process.poll() is not None:
        raise RuntimeError(f"process {process.pid} exited before {marker} snapshot")
    os.kill(process.pid, signal.SIGUSR1)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summaries = (
            parse_counter_summaries(path.read_text(encoding="utf-8", errors="replace"), marker)
            if path.exists()
            else []
        )
        if len(summaries) > len(before):
            return summaries[-1]
        if process.poll() is not None:
            raise RuntimeError(f"process {process.pid} exited while waiting for {marker}")
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {marker} in {path}")


def load_benchmark(root: Path):
    os.environ["KUASAR_BENCH_ROOT"] = str(root)
    spec = importlib.util.spec_from_file_location("benchmark_worker_base", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark helper: {BASE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def minimal_result_for_test(vm_count: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "round": 1,
        "execution_order": 1,
        "mode": "lazy-pmem",
        "cache_backing": "file",
        "source_state": "plaintext-warm",
        "vm_count": vm_count,
        "image": "nginx-1.27.3-alpine",
        "manifest_key": "a" * 64,
        "manifest_image_bytes": 4096,
        "capture_order": list(CAPTURE_ORDER),
        "group": {
            "application_ready_seconds": 1.0,
            "operation_complete_seconds": 2.0,
        },
        "workload": {
            "name": "nginx-first-request",
            "result_kind": "sha256",
            "response_sha256": "b" * 64,
            "response_bytes": 615,
        },
        "vms": [{"vm_index": index} for index in range(1, vm_count + 1)],
        "metrics": {},
        "cache": {},
        "counters": {
            "accelerator": {},
            "lazyd": {},
            "cloud_hypervisor": {},
            "vhost_root": {},
        },
    }


def validate_worker_result(result: dict[str, object]) -> None:
    if result.get("schema_version") != 1:
        raise ValueError("unsupported worker result schema")
    if result.get("capture_order") != list(CAPTURE_ORDER):
        raise ValueError("worker capture order is incomplete or invalid")
    vm_count = int(result.get("vm_count", 0))
    vms = result.get("vms")
    if vm_count <= 0 or not isinstance(vms, list) or len(vms) != vm_count:
        raise ValueError("worker VM rows do not match vm_count")
    workload = result.get("workload")
    if not isinstance(workload, dict) or workload.get("name") not in WORKLOADS:
        raise ValueError("worker result has invalid workload")
    result_kind = workload.get("result_kind")
    if result_kind == "sha256":
        if not SHA256.fullmatch(str(workload.get("response_sha256", ""))):
            raise ValueError("worker result has invalid response SHA-256")
        if int(workload.get("response_bytes", 0)) <= 0:
            raise ValueError("worker result has empty response")
    elif result_kind == "byte-count":
        byte_counts = workload.get("bytes_per_vm")
        if (
            not isinstance(byte_counts, list)
            or len(byte_counts) != vm_count
            or any(int(value) <= 0 for value in byte_counts)
            or len({int(value) for value in byte_counts}) != 1
        ):
            raise ValueError("worker result has inconsistent byte counts")
    elif result_kind == "capability":
        capabilities = workload.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or len(capabilities) != vm_count
            or any(not str(value) for value in capabilities)
            or len({str(value) for value in capabilities}) != 1
        ):
            raise ValueError("worker result has inconsistent capabilities")
    else:
        raise ValueError("worker result has invalid workload result kind")
    if result.get("source_state") not in SOURCE_STATES:
        raise ValueError("worker result has invalid source state")
    if result.get("cache_backing") not in CACHE_BACKINGS:
        raise ValueError("worker result has invalid cache backing")
    counters = result.get("counters")
    if not isinstance(counters, dict) or set(counters) != {
        "accelerator",
        "lazyd",
        "cloud_hypervisor",
        "vhost_root",
    }:
        raise ValueError("worker result has incomplete counters")


def cache_checkpoint(benchmark, base, cache_root: Path | None) -> dict[str, object]:
    if cache_root is None:
        return {"allocated_bytes": 0, "ready": None}
    state: dict[str, object] = {"allocated_bytes": base.allocated_tree(cache_root)}
    try:
        state["ready"] = benchmark.cache_snapshot(cache_root)
    except RuntimeError:
        state["ready"] = None
    return state


def group_barriers(vms) -> dict[str, float]:
    group_start = min(vm.start_ns for vm in vms)

    def latest(name: str) -> float:
        return (max(vm.observations_ns[name] for vm in vms) - group_start) / 1_000_000_000

    return {
        "cloud_hypervisor_started_seconds": latest("ch_started"),
        "launch_ack_seconds": latest("launch_ack"),
        "application_ready_seconds": latest("app_ready"),
        "operation_begin_seconds": latest("read_begin"),
        "operation_complete_seconds": latest("read_end"),
        "first_operation_max_seconds": max(vm.stage_seconds("read_begin", "read_end") for vm in vms),
    }


def workload_result(vm, result_kind: str) -> str | int:
    if result_kind == "sha256":
        if vm.workload_result_sha256 is None or vm.data_bytes is None:
            raise RuntimeError("workload SHA-256 result is incomplete")
        return vm.workload_result_sha256
    if result_kind == "byte-count":
        if vm.data_bytes is None:
            raise RuntimeError("workload byte-count result is missing")
        return int(vm.data_bytes)
    if result_kind == "capability":
        if vm.workload_capability is None:
            raise RuntimeError("workload capability result is missing")
        return vm.workload_capability
    raise ValueError(f"unknown workload result kind: {result_kind}")


def workload_result_fields(result_kind: str, results: list[str | int], vms) -> dict:
    if result_kind == "sha256":
        response_bytes = {vm.data_bytes for vm in vms}
        if len(response_bytes) != 1 or None in response_bytes:
            raise RuntimeError("response byte counts differ within benchmark group")
        return {
            "response_sha256": str(results[0]),
            "response_bytes": int(next(iter(response_bytes))),
        }
    if result_kind == "byte-count":
        return {"bytes_per_vm": [int(value) for value in results]}
    if result_kind == "capability":
        return {"capabilities": [str(value) for value in results]}
    raise ValueError(f"unknown workload result kind: {result_kind}")


def run_worker(
    *,
    root: Path,
    round_number: int,
    execution_order: int,
    mode: str,
    source_state: str,
    vm_count: int,
    cache_backing: str,
    workload_name: str,
) -> dict[str, object]:
    if source_state not in SOURCE_STATES:
        raise ValueError(f"unknown source state: {source_state}")
    if cache_backing not in CACHE_BACKINGS:
        raise ValueError(f"unknown cache backing: {cache_backing}")
    if workload_name not in WORKLOADS:
        raise ValueError(f"unknown workload: {workload_name}")
    os.environ["LAZYD_CACHE_BACKING"] = cache_backing
    benchmark = load_benchmark(root)
    if mode not in benchmark.EVIDENCE_MODES:
        raise ValueError(f"unknown evidence mode: {mode}")
    cgroup = current_cgroup_path()
    if cgroup == Path("/sys/fs/cgroup"):
        raise RuntimeError("benchmark worker must run in a dedicated cgroup-v2 service")
    memory = CgroupMemoryTracker(cgroup)
    accounting = CgroupAccountingTracker(cgroup)
    capture_order: list[str] = []
    cache_states: dict[str, object] = {}
    cache_root: Path | None = None
    base = None

    def capture(name: str) -> None:
        memory.capture(name)
        accounting.capture(name)
        capture_order.append(name)
        cache_states[name] = cache_checkpoint(benchmark, base, cache_root) if base is not None else {
            "allocated_bytes": 0,
            "ready": None,
        }

    capture("worker_baseline")
    reset_memory_peak(cgroup)

    benchmark.RUNTIME.mkdir(parents=True, exist_ok=True)
    benchmark.LOGS.mkdir(parents=True, exist_ok=True)
    runner = benchmark.load_runner()
    base = runner.load_base_runner()
    workload_contract = benchmark.build_named_workload(workload_name)
    image = next(
        item
        for item in runner.load_images()
        if item.name == workload_contract.image_name
    )
    mode_tag = {
        benchmark.MODE_BLK: "b",
        benchmark.MODE_BLK_SHARED: "s",
        benchmark.MODE_PMEM: "p",
    }[mode]
    state_tag = "c" if source_state == "plaintext-cold" else "w"
    backing_tag = "f" if cache_backing == "file" else "m"
    phase = (
        f"cb{round_number:02d}{execution_order}{state_tag}{mode_tag}{backing_tag}{vm_count}-"
        f"{time.monotonic_ns() % 1_000_000}"
    )
    sockets = base.short_socket_dir(phase)
    sockets.mkdir(parents=True, exist_ok=False)
    service_runtime = benchmark.RUNTIME / phase / "lazyd-service"
    service_logs = benchmark.LOGS / phase / "lazyd-service"
    service_runtime.mkdir(parents=True, exist_ok=False)
    service_logs.mkdir(parents=True, exist_ok=False)
    lazyd_process = None
    vms = []
    warm_result = None
    mapping = None
    pss = None
    result = None
    accelerator_baseline = None
    lazyd_baseline = None
    lazyd_log = service_logs / "lazyd.log"
    try:
        with base.PhaseContext(phase, vm_count) as context:
            if mode in (benchmark.MODE_BLK_SHARED, benchmark.MODE_PMEM):
                lazyd_process, cache_root, control_socket, data_socket = base.start_lazyd(
                    service_runtime, sockets, service_logs, None
                )
            else:
                control_socket = data_socket = None

            if source_state == "plaintext-warm":
                warm_vm = benchmark.start_vm(
                    runner,
                    base,
                    context,
                    image,
                    phase=phase,
                    mode=mode,
                    index=0,
                    tap=context.taps[0],
                    range_socket=context.range_socket,
                    control_socket=control_socket,
                    data_socket=data_socket,
                    working_set_bytes=image.erofs_data_bytes,
                    verify=False,
                    workload_name=workload_name,
                )
                try:
                    warm_vm.wait_ready()
                    warm_result = workload_result(warm_vm, workload_contract.result_kind)
                finally:
                    warm_vm.stop()

                accelerator_baseline = request_counter_snapshot(
                    context.range_process,
                    context.log_dir / "manifest-range.log",
                    ACCELERATOR_COUNTER_MARKER,
                )
                if lazyd_process is not None:
                    lazyd_baseline = request_counter_snapshot(
                        lazyd_process,
                        lazyd_log,
                        LAZYD_COUNTER_MARKER,
                    )

            capture("prelaunch")
            for index in range(1, vm_count + 1):
                vms.append(
                    benchmark.start_vm(
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
                        working_set_bytes=image.erofs_data_bytes,
                        verify=False,
                        workload_name=workload_name,
                    )
                )
            for vm in vms:
                vm.wait_observation("app_ready")
            capture("app_ready")
            for vm in vms:
                vm.wait_ready()
            capture("operation_complete")
            time.sleep(1)
            capture("held")

            measured_results = [
                workload_result(vm, workload_contract.result_kind) for vm in vms
            ]
            if len(set(measured_results)) != 1:
                raise RuntimeError("workload results differ within benchmark group")
            measured_result = measured_results[0]
            if warm_result is not None and warm_result != measured_result:
                raise RuntimeError("warmup and measured workload results differ")

            families = [benchmark.process_family_metrics(vm.process.pid, vm.ch_pid) for vm in vms]
            lazyd_pss = (
                benchmark.smaps_rollup(lazyd_process.pid).get("Pss", 0)
                if lazyd_process is not None
                else 0
            )
            pss = {
                "sandbox_families_kib": sum(int(item["pss_kib"]) for item in families),
                "lazyd_kib": lazyd_pss,
                "total_kib": sum(int(item["pss_kib"]) for item in families) + lazyd_pss,
            }
            if mode == benchmark.MODE_PMEM:
                mapping = benchmark.selected_mapping_totals(base, vms)

            result = {
                "schema_version": 1,
                "round": round_number,
                "execution_order": execution_order,
                "mode": mode,
                "cache_backing": cache_backing,
                "source_state": source_state,
                "vm_count": vm_count,
                "image": image.name,
                "manifest_key": image.manifest_key,
                "manifest_image_bytes": image.manifest_image_bytes,
                "capture_order": capture_order,
                "group": group_barriers(vms),
                "workload": {
                    "name": workload_name,
                    "result_kind": workload_contract.result_kind,
                    **workload_result_fields(
                        workload_contract.result_kind, measured_results, vms
                    ),
                },
                "vms": [
                    {
                        "vm_index": vm.index,
                        "host_to_ch_seconds": vm.since_start_seconds("ch_started"),
                        "ch_to_launch_seconds": vm.stage_seconds("ch_started", "launch_ack"),
                        "launch_to_app_seconds": vm.stage_seconds("launch_ack", "app_ready"),
                        "first_operation_seconds": vm.stage_seconds("read_begin", "read_end"),
                    }
                    for vm in vms
                ],
                "metrics": {**memory.to_columns(), **accounting.to_columns()},
                "cache": cache_states,
                "secondary_pss": pss,
                "pmem_mappings": mapping,
            }

            for vm in reversed(vms):
                vm.stop()

            ch_snapshots = []
            for vm in vms:
                summaries = parse_counter_summaries(
                    (vm.log_dir / "sandbox.stderr.log").read_text(
                        encoding="utf-8", errors="replace"
                    ),
                    CH_COUNTER_MARKER,
                )
                if summaries:
                    ch_snapshots.append(summaries[-1])
            if mode == benchmark.MODE_PMEM and len(ch_snapshots) != vm_count:
                raise RuntimeError(
                    f"received {len(ch_snapshots)} Cloud Hypervisor summaries for {vm_count} VMs"
                )
            ch_final = sum_counters(ch_snapshots)
            ch_baseline = {name: 0 for name in ch_final}
            result["counters"] = {
                "cloud_hypervisor": {
                    "baseline": ch_baseline,
                    "final": ch_final,
                    "measured": ch_final,
                },
                "vhost_root": aggregate_vhost_root_stats(
                    [vm.log_dir / "stats.json" for vm in vms]
                ),
            }

            base.close_backend_process(lazyd_process)
            lazyd_process = None

        if result is None:
            raise RuntimeError("benchmark worker did not produce a result")
        result["counters"]["accelerator"] = measured_counter_summary(
            context.log_dir / "manifest-range.log",
            ACCELERATOR_COUNTER_MARKER,
            accelerator_baseline,
        )
        result["counters"]["lazyd"] = (
            measured_counter_summary(lazyd_log, LAZYD_COUNTER_MARKER, lazyd_baseline)
            if mode in (benchmark.MODE_BLK_SHARED, benchmark.MODE_PMEM)
            else {}
        )
        validate_worker_result(result)
        return result
    finally:
        for vm in reversed(vms):
            vm.stop()
        base.close_backend_process(lazyd_process)
        if sockets.exists():
            shutil.rmtree(sockets)
        if service_runtime.exists():
            shutil.rmtree(service_runtime)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--execution-order", type=int, required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--source-state", choices=SOURCE_STATES, required=True)
    parser.add_argument("--vm-count", type=int, required=True)
    parser.add_argument("--cache-backing", choices=CACHE_BACKINGS, required=True)
    parser.add_argument(
        "--workload", choices=WORKLOADS, default="nginx-first-request"
    )
    args = parser.parse_args()
    if args.round <= 0 or args.execution_order <= 0 or args.vm_count <= 0:
        parser.error("round, execution-order, and vm-count must be positive")
    result = run_worker(
        root=args.root.resolve(),
        round_number=args.round,
        execution_order=args.execution_order,
        mode=args.mode,
        source_state=args.source_state,
        vm_count=args.vm_count,
        cache_backing=args.cache_backing,
        workload_name=args.workload,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
