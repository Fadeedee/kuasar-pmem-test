import tempfile
import unittest
from pathlib import Path

from benchmark_metrics import (
    CgroupAccountingSnapshot,
    CgroupAccountingTracker,
    CgroupMemoryTracker,
    CgroupMemorySnapshot,
    cgroup_path_from_proc,
    parse_cpu_stat,
    parse_counter_summaries,
    parse_io_stat,
    parse_memory_stat,
    reset_memory_peak,
    subtract_counters,
    sum_counters,
)


class BenchmarkMetricsTest(unittest.TestCase):
    def test_structured_counter_markers_parse_delta_and_sum(self):
        marker = "KUASAR_BENCH_LAZYD_STATS"
        summaries = parse_counter_summaries(
            "noise before\n"
            f'{marker}={{"fetch_requests":2,"materialized_bytes":4096}}\n'
            f'2026 INFO {marker}={{"fetch_requests":5,"materialized_bytes":12288}}\n',
            marker,
        )
        self.assertEqual(
            summaries,
            [
                {"fetch_requests": 2, "materialized_bytes": 4096},
                {"fetch_requests": 5, "materialized_bytes": 12288},
            ],
        )
        self.assertEqual(
            subtract_counters(summaries[-1], summaries[0]),
            {"fetch_requests": 3, "materialized_bytes": 8192},
        )
        self.assertEqual(
            sum_counters(
                [
                    {"fetch_requests": 1, "materialized_bytes": 4096},
                    {"fetch_requests": 2, "materialized_bytes": 8192},
                ]
            ),
            {"fetch_requests": 3, "materialized_bytes": 12288},
        )

    def test_counter_parser_rejects_non_integer_or_decreasing_values(self):
        with self.assertRaisesRegex(ValueError, "non-negative integers"):
            parse_counter_summaries(
                'KUASAR_BENCH={"fetch_requests":"1"}\n', "KUASAR_BENCH"
            )
        with self.assertRaisesRegex(ValueError, "decreased"):
            subtract_counters({"fetch_requests": 1}, {"fetch_requests": 2})

    def test_cpu_and_io_accounting_parsers_sum_devices(self):
        self.assertEqual(
            parse_cpu_stat("usage_usec 30\nuser_usec 20\nsystem_usec 10\n"),
            {"usage_usec": 30, "user_usec": 20, "system_usec": 10},
        )
        self.assertEqual(
            parse_io_stat(
                "8:0 rbytes=10 wbytes=20 rios=1 wios=2\n"
                "8:16 rbytes=30 wbytes=40 rios=3 wios=4\n"
            ),
            {"rbytes": 40, "wbytes": 60, "rios": 4, "wios": 6},
        )

    def test_accounting_tracker_exports_baseline_deltas(self):
        path = Path("/sys/fs/cgroup/sample")
        tracker = CgroupAccountingTracker(path)
        values = iter(
            CgroupAccountingSnapshot(
                path=path,
                captured_ns=index,
                cpu={"usage_usec": cpu, "user_usec": cpu - 1, "system_usec": 1},
                io={"rbytes": read, "wbytes": 0, "rios": 1, "wios": 0},
            )
            for index, cpu, read in (
                (1, 10, 100),
                (2, 20, 110),
                (3, 30, 130),
                (4, 50, 170),
                (5, 60, 180),
            )
        )
        for checkpoint in CgroupAccountingTracker.CHECKPOINTS:
            tracker.capture(checkpoint, capture=lambda: next(values))

        columns = tracker.to_columns()
        self.assertEqual(columns["held_cpu_usage_usec"], 60)
        self.assertEqual(columns["held_delta_cpu_usage_usec"], 50)
        self.assertEqual(columns["operation_complete_delta_io_rbytes"], 70)

    def test_cgroup_path_from_unified_proc_entry(self):
        mount = Path("/sys/fs/cgroup")

        path = cgroup_path_from_proc(
            "0::/system.slice/kuasar-benchmark.service\n", mount
        )

        self.assertEqual(path, mount / "system.slice/kuasar-benchmark.service")

    def test_cgroup_path_rejects_non_unified_or_escaping_entries(self):
        mount = Path("/sys/fs/cgroup")

        with self.assertRaisesRegex(RuntimeError, "unified cgroup-v2"):
            cgroup_path_from_proc("2:memory:/legacy\n", mount)
        with self.assertRaisesRegex(RuntimeError, "outside cgroup mount"):
            cgroup_path_from_proc("0::/../../outside\n", mount)

    def test_parse_memory_stat_keeps_known_fields_and_defaults_missing_fields(self):
        stats = parse_memory_stat("anon 4096\nfile 8192\nslab 512\nunknown 7\n")

        self.assertEqual(stats["anon"], 4096)
        self.assertEqual(stats["file"], 8192)
        self.assertEqual(stats["slab"], 512)
        self.assertEqual(stats["kernel"], 0)
        self.assertEqual(stats["pagetables"], 0)
        self.assertEqual(stats["shmem"], 0)
        self.assertNotIn("unknown", stats)

    def test_snapshot_reads_cgroup_files_and_serializes_stable_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            cgroup = Path(directory)
            (cgroup / "memory.current").write_text("12288\n", encoding="ascii")
            (cgroup / "memory.peak").write_text("16384\n", encoding="ascii")
            (cgroup / "memory.stat").write_text(
                "anon 4096\nfile 6144\nkernel 2048\npagetables 512\nslab 256\nshmem 0\n",
                encoding="ascii",
            )

            snapshot = CgroupMemorySnapshot.capture(cgroup, captured_ns=123)

        self.assertEqual(snapshot.current_bytes, 12288)
        self.assertEqual(snapshot.peak_bytes, 16384)
        self.assertEqual(snapshot.captured_ns, 123)
        self.assertEqual(
            snapshot.to_columns("held"),
            {
                "held_cgroup_path": str(cgroup),
                "held_captured_ns": 123,
                "held_memory_current_bytes": 12288,
                "held_memory_peak_bytes": 16384,
                "held_memory_anon_bytes": 4096,
                "held_memory_file_bytes": 6144,
                "held_memory_kernel_bytes": 2048,
                "held_memory_pagetables_bytes": 512,
                "held_memory_slab_bytes": 256,
                "held_memory_shmem_bytes": 0,
            },
        )

    def test_delta_preserves_reclaim_and_requires_same_cgroup(self):
        baseline = CgroupMemorySnapshot(
            path=Path("/sys/fs/cgroup/sample"),
            captured_ns=1,
            current_bytes=100,
            peak_bytes=120,
            stats={
                "anon": 40,
                "file": 50,
                "kernel": 10,
                "pagetables": 4,
                "slab": 3,
                "shmem": 0,
            },
        )
        current = CgroupMemorySnapshot(
            path=baseline.path,
            captured_ns=2,
            current_bytes=150,
            peak_bytes=180,
            stats={
                "anon": 80,
                "file": 45,
                "kernel": 25,
                "pagetables": 8,
                "slab": 9,
                "shmem": 0,
            },
        )

        delta = current.delta(baseline)

        self.assertEqual(delta.current_bytes, 50)
        self.assertEqual(delta.peak_bytes, 60)
        self.assertEqual(delta.stats["file"], -5)
        with self.assertRaisesRegex(ValueError, "same cgroup"):
            current.delta(
                CgroupMemorySnapshot(
                    path=Path("/sys/fs/cgroup/other"),
                    captured_ns=1,
                    current_bytes=0,
                    peak_bytes=0,
                    stats={name: 0 for name in current.stats},
                )
            )

    def test_reset_memory_peak_writes_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            peak = Path(directory) / "memory.peak"
            peak.write_text("123\n", encoding="ascii")

            reset_memory_peak(Path(directory))

            self.assertEqual(peak.read_text(encoding="ascii"), "0\n")

    def test_tracker_enforces_checkpoint_order_and_exports_baseline_deltas(self):
        tracker = CgroupMemoryTracker(Path("/sys/fs/cgroup/sample"))
        values = iter(
            [
                CgroupMemorySnapshot(
                    path=tracker.path,
                    captured_ns=index,
                    current_bytes=current,
                    peak_bytes=peak,
                    stats={
                        "anon": current,
                        "file": 0,
                        "kernel": 0,
                        "pagetables": 0,
                        "slab": 0,
                        "shmem": 0,
                    },
                )
                for index, current, peak in (
                    (1, 100, 100),
                    (2, 120, 120),
                    (3, 160, 180),
                    (4, 190, 210),
                    (5, 180, 210),
                )
            ]
        )

        for checkpoint in CgroupMemoryTracker.CHECKPOINTS:
            tracker.capture(checkpoint, capture=lambda: next(values))

        columns = tracker.to_columns()
        self.assertEqual(columns["worker_baseline_memory_current_bytes"], 100)
        self.assertEqual(columns["held_memory_current_bytes"], 180)
        self.assertEqual(columns["held_delta_memory_current_bytes"], 80)
        self.assertEqual(columns["operation_complete_delta_memory_peak_bytes"], 110)
        with self.assertRaisesRegex(ValueError, "already captured"):
            tracker.capture("held", capture=lambda: next(values))

    def test_tracker_rejects_skipped_or_unknown_checkpoints(self):
        tracker = CgroupMemoryTracker(Path("/sys/fs/cgroup/sample"))

        with self.assertRaisesRegex(ValueError, "expected worker_baseline"):
            tracker.capture("prelaunch")
        with self.assertRaisesRegex(ValueError, "unknown memory checkpoint"):
            tracker.capture("invented")


if __name__ == "__main__":
    unittest.main()
