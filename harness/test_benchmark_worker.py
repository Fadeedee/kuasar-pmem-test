import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_benchmark_worker.py")


def load_module():
    spec = importlib.util.spec_from_file_location("benchmark_worker", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BenchmarkWorkerTest(unittest.TestCase):
    def test_measured_counter_summary_subtracts_warmup_baseline(self):
        worker = load_module()
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "lazyd.log"
            log.write_text(
                'KUASAR_BENCH_LAZYD_STATS={"fetch_requests":2,"materialized_bytes":4096}\n'
                'KUASAR_BENCH_LAZYD_STATS={"fetch_requests":5,"materialized_bytes":12288}\n',
                encoding="utf-8",
            )
            got = worker.measured_counter_summary(
                log,
                worker.LAZYD_COUNTER_MARKER,
                {"fetch_requests": 2, "materialized_bytes": 4096},
            )

        self.assertEqual(
            got["measured"],
            {"fetch_requests": 3, "materialized_bytes": 8192},
        )
        self.assertEqual(got["final"]["fetch_requests"], 5)

    def test_aggregate_vhost_root_stats_uses_only_blk0(self):
        worker = load_module()
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, count in enumerate((3, 5)):
                path = Path(directory) / f"stats-{index}.json"
                path.write_text(
                    json.dumps(
                        {
                            "backends": [
                                {
                                    "name": "blk0",
                                    "loaded_blocks": count + 1,
                                    "total_blocks": 100,
                                    "read": {"count": count, "bytes": count * 4096, "err_count": 0},
                                },
                                {
                                    "name": "blk1",
                                    "loaded_blocks": 9,
                                    "total_blocks": 200,
                                    "read": {"count": 99, "bytes": 99, "err_count": 1},
                                },
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                paths.append(path)

            got = worker.aggregate_vhost_root_stats(paths)

        self.assertEqual(
            got,
            {
                "backend_count": 2,
                "read_requests": 8,
                "read_bytes": 8 * 4096,
                "read_errors": 0,
                "loaded_blocks": 10,
                "total_blocks": 200,
            },
        )

    def test_result_schema_requires_ordered_cgroup_checkpoints(self):
        worker = load_module()
        result = {
            "schema_version": 1,
            "round": 1,
            "execution_order": 2,
            "mode": "lazy-pmem",
            "cache_backing": "file",
            "source_state": "plaintext-warm",
            "vm_count": 2,
            "image": "nginx-1.27.3-alpine",
            "manifest_key": "a" * 64,
            "manifest_image_bytes": 4096,
            "capture_order": list(worker.CAPTURE_ORDER),
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
            "vms": [{"vm_index": 1}, {"vm_index": 2}],
            "metrics": {},
            "cache": {},
            "counters": {
                "accelerator": {},
                "lazyd": {},
                "cloud_hypervisor": {},
                "vhost_root": {},
            },
        }

        worker.validate_worker_result(result)
        result["capture_order"] = ["worker_baseline", "app_ready"]
        with self.assertRaisesRegex(ValueError, "capture order"):
            worker.validate_worker_result(result)

    def test_result_schema_rejects_missing_vm_or_bad_digest(self):
        worker = load_module()
        result = worker.minimal_result_for_test(vm_count=2)

        result["vms"] = [{"vm_index": 1}]
        with self.assertRaisesRegex(ValueError, "VM rows"):
            worker.validate_worker_result(result)

        result = worker.minimal_result_for_test(vm_count=1)
        result["workload"]["response_sha256"] = "bad"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            worker.validate_worker_result(result)

    def test_result_schema_accepts_full_tree_byte_counts(self):
        worker = load_module()
        result = worker.minimal_result_for_test(vm_count=2)
        result["image"] = "openeuler-24.03-lts"
        result["workload"] = {
            "name": "full-tree-scan",
            "result_kind": "byte-count",
            "bytes_per_vm": [4096, 4096],
        }

        worker.validate_worker_result(result)
        result["workload"]["bytes_per_vm"] = [4096, 8192]
        with self.assertRaisesRegex(ValueError, "byte counts"):
            worker.validate_worker_result(result)


if __name__ == "__main__":
    unittest.main()
