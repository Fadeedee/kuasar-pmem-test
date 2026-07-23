import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_reuse_benchmark.py")


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError(f"benchmark module is missing: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("reuse_benchmark", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReuseBenchmarkTest(unittest.TestCase):
    def test_summary_contains_tail_latency(self):
        benchmark = load_module()

        summary = benchmark.summarize([float(value) for value in range(1, 31)])

        self.assertEqual(summary["count"], 30)
        self.assertEqual(summary["p50"], 15.5)
        self.assertEqual(summary["p90"], 27.1)
        self.assertAlmostEqual(summary["p95"], 28.55)

    def test_paired_comparison_counts_pmem_wins(self):
        benchmark = load_module()
        rows = [
            {"round": 1, "mode": benchmark.MODE_PMEM, "read_seconds": 1.0},
            {"round": 1, "mode": benchmark.MODE_BLK, "read_seconds": 2.0},
            {"round": 2, "mode": benchmark.MODE_PMEM, "read_seconds": 3.0},
            {"round": 2, "mode": benchmark.MODE_BLK, "read_seconds": 2.5},
            {"round": 3, "mode": benchmark.MODE_PMEM, "read_seconds": 1.5},
            {"round": 3, "mode": benchmark.MODE_BLK, "read_seconds": 3.0},
        ]

        comparison = benchmark.paired_comparison(rows, "read_seconds")

        self.assertEqual(comparison["pairs"], 3)
        self.assertEqual(comparison["pmem_wins"], 2)
        self.assertAlmostEqual(comparison["win_rate_percent"], 200 / 3)

    def test_workloads_only_differ_by_transport_device(self):
        benchmark = load_module()

        pmem = benchmark.build_workload(benchmark.MODE_PMEM, 128 * 1024 * 1024)
        blk = benchmark.build_workload(benchmark.MODE_BLK, 128 * 1024 * 1024)
        shared = benchmark.build_workload(
            benchmark.MODE_BLK_SHARED, 128 * 1024 * 1024
        )

        self.assertIn("/dev/pmem1", pmem)
        self.assertIn("/dev/vda", blk)
        self.assertEqual(shared, blk)
        self.assertEqual(pmem.replace("/dev/pmem1", "DEVICE"), blk.replace("/dev/vda", "DEVICE"))
        self.assertIn("KUASAR_REUSE_READ_BEGIN", pmem)
        self.assertIn("KUASAR_REUSE_READY", pmem)

    def test_shared_cache_mode_uses_product_config_without_benchmark_environment(self):
        benchmark = load_module()

        config_mode, env = benchmark.transport_launch_contract(
            benchmark.MODE_BLK_SHARED,
            control_socket=Path("/tmp/control.sock"),
            data_socket=Path("/tmp/data.sock"),
            range_socket=Path("/tmp/range.sock"),
            base_env={"PATH": "/bin"},
        )

        self.assertEqual(config_mode, benchmark.MODE_BLK_SHARED)
        self.assertNotIn("KUASAR_BENCH_SHARED_CACHE_BLK", env)
        self.assertNotIn("KUASAR_BENCH_LAZYD_DATA_SOCKET", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_direct_workload_prewarms_before_measured_read(self):
        benchmark = load_module()

        pmem = benchmark.build_direct_workload(benchmark.MODE_PMEM, 64 * 1024 * 1024)
        blk = benchmark.build_direct_workload(benchmark.MODE_BLK, 64 * 1024 * 1024)

        self.assertEqual(pmem.replace("/dev/pmem1", "DEVICE"), blk.replace("/dev/vda", "DEVICE"))
        self.assertEqual(pmem.count("iflag=direct"), 2)
        self.assertLess(pmem.index("iflag=direct"), pmem.index("KUASAR_REUSE_READ_BEGIN"))
        self.assertIn("KUASAR_REUSE_BYTES=67108864", pmem)

    def test_filesystem_workload_invokes_read_tree_between_markers(self):
        benchmark = load_module()

        command = benchmark.build_filesystem_workload()

        self.assertIn("/opt/sandbox-runtime/bin/read-tree", command)
        self.assertNotIn("find ", command)
        self.assertIn("KUASAR_APP_READY", command)
        self.assertIn("KUASAR_REUSE_READ_BEGIN", command)
        self.assertIn("KUASAR_REUSE_READ_END", command)
        self.assertLess(
            command.index("KUASAR_REUSE_READ_BEGIN"),
            command.index("/opt/sandbox-runtime/bin/read-tree"),
        )
        self.assertLess(
            command.index("/opt/sandbox-runtime/bin/read-tree"),
            command.index("KUASAR_REUSE_READ_END"),
        )

    def test_named_workload_uses_shared_application_contract(self):
        benchmark = load_module()

        workload = benchmark.build_named_workload("nginx-first-request", hold_seconds=9)

        self.assertEqual(workload.image_name, "nginx-1.27.3-alpine")
        self.assertIn("KUASAR_BENCH_APP_READY", workload.command)
        self.assertIn("KUASAR_BENCH_OPERATION_BEGIN", workload.command)
        self.assertIn("KUASAR_BENCH_OPERATION_END", workload.command)
        self.assertIn("KUASAR_BENCH_READY", workload.command)

    def test_first_touch_workload_has_two_separately_measured_traversals(self):
        benchmark = load_module()

        command = benchmark.build_first_touch_workload()

        self.assertEqual(command.count("/opt/sandbox-runtime/bin/read-tree"), 2)
        for marker in (
            "KUASAR_APP_READY",
            "KUASAR_FIRST_TOUCH_BEGIN",
            "KUASAR_FIRST_TOUCH_END",
            "KUASAR_REPEAT_READ_BEGIN",
            "KUASAR_REPEAT_READ_END",
            "KUASAR_REUSE_READY",
        ):
            self.assertIn(marker, command)
        self.assertLess(command.index("KUASAR_APP_READY"), command.index("KUASAR_FIRST_TOUCH_BEGIN"))
        self.assertLess(command.index("KUASAR_FIRST_TOUCH_END"), command.index("KUASAR_REPEAT_READ_BEGIN"))

    def test_observation_name_recognizes_host_and_guest_stages(self):
        benchmark = load_module()

        cases = {
            b"lazy root prepared: instance=x": "lazy_prepare",
            b"CH started pid=123": "ch_started",
            b"launch: launch_ack received": "launch_ack",
            b"KUASAR_APP_READY": "app_ready",
            b"KUASAR_REUSE_READ_BEGIN": "read_begin",
            b"KUASAR_REUSE_READ_END": "read_end",
            b"KUASAR_FIRST_TOUCH_BEGIN": "first_touch_begin",
            b"KUASAR_FIRST_TOUCH_END": "first_touch_end",
            b"KUASAR_REPEAT_READ_BEGIN": "repeat_read_begin",
            b"KUASAR_REPEAT_READ_END": "repeat_read_end",
            b"KUASAR_BENCH_APP_READY": "app_ready",
            b"KUASAR_BENCH_OPERATION_BEGIN": "read_begin",
            b"KUASAR_BENCH_OPERATION_END": "read_end",
            b"KUASAR_BENCH_READY": "ready",
        }

        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(benchmark.observation_name(line), expected)
        self.assertIsNone(benchmark.observation_name(b"unrelated log line"))

    def test_stage_seconds_rejects_missing_or_reversed_observations(self):
        benchmark = load_module()

        observations = {"begin": 1_000_000_000, "end": 3_500_000_000}

        self.assertEqual(benchmark.stage_seconds(observations, "begin", "end"), 2.5)
        with self.assertRaisesRegex(RuntimeError, "missing observation"):
            benchmark.stage_seconds(observations, "unknown", "end")
        with self.assertRaisesRegex(RuntimeError, "precedes"):
            benchmark.stage_seconds(observations, "end", "begin")


if __name__ == "__main__":
    unittest.main()
