import random
import tempfile
import unittest
from pathlib import Path

from analyze_three_path_evidence import (
    MetricSpec,
    build_paired_comparisons,
    format_improvement,
    quantile,
    svg_line_chart,
    validate_sample_grid,
)


MODES = (
    "vhost-user-blk",
    "vhost-user-blk-shared-cache",
    "lazy-pmem",
)


def sample(round_number: int, mode: str, ready: float) -> dict:
    accelerator_bytes = 0 if mode == "vhost-user-blk" else 4096
    lazyd = (
        {}
        if mode == "vhost-user-blk"
        else {
            "measured": {
                "fetch_requests": 1,
                "fetch_request_bytes": 4096,
                "fetch_returned_range_bytes": 4096,
                "ready_hits": 0,
                "ready_misses": 1,
                "materialized_ranges": 1,
                "materialized_bytes": 4096,
                "materialized_max_bytes": 4096,
            }
        }
    )
    cloud_hypervisor = (
        {
            "measured": {
                "data_faults": 1,
                "padding_faults": 0,
                "fetch_requests": 1,
                "fetch_request_bytes": 4096,
                "mmap_ranges": 1,
                "mmap_bytes": 4096,
                "wakes": 1,
            }
        }
        if mode == "lazy-pmem"
        else {"measured": {}}
    )
    return {
        "round": round_number,
        "mode": mode,
        "source_state": "plaintext-cold",
        "vm_count": 1,
        "group": {
            "application_ready_seconds": ready,
            "first_operation_max_seconds": 0.001,
        },
        "workload": {"response_sha256": "same", "response_bytes": 4},
        "metrics": {
            "worker_baseline_cgroup_path": (
                f"/sys/fs/cgroup/system.slice/klp-e-r{round_number}-{mode}.service"
            ),
            "held_delta_memory_current_bytes": 1024,
            "app_ready_delta_cpu_usage_usec": 100,
            "app_ready_delta_io_rbytes": 0,
            "app_ready_delta_io_wbytes": 0,
        },
        "secondary_pss": {"total_kib": 1},
        "pmem_mappings": (
            {
                "mapped_rss_kib": 4,
                "mapped_pss_kib": 4,
                "mapped_shared_clean_kib": 0,
                "mapped_private_dirty_kib": 0,
                "mapping_identities": [["08:20", 1]],
            }
            if mode == "lazy-pmem"
            else None
        ),
        "counters": {
            "cloud_hypervisor": cloud_hypervisor,
            "vhost_root": {
                "backend_count": 1,
                "read_requests": 1,
                "read_bytes": 4096,
                "read_errors": 0,
                "loaded_blocks": 1,
                "total_blocks": 1,
            },
            "accelerator": {
                "measured": {
                    "describe_requests": 0 if mode == "vhost-user-blk" else 1,
                    "read_range_requests": 0 if mode == "vhost-user-blk" else 1,
                    "read_range_bytes": accelerator_bytes,
                    "read_range_max_bytes": accelerator_bytes,
                }
            },
            "lazyd": lazyd,
        },
    }


class AnalyzeThreePathEvidenceTests(unittest.TestCase):
    def test_quantile_uses_linear_interpolation(self) -> None:
        self.assertEqual(quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertAlmostEqual(quantile([1, 2, 3, 4], 0.95), 3.85)

    def test_improvement_format_does_not_report_negative_reduction(self) -> None:
        self.assertEqual(format_improvement(8.25), "降低 **8.2%**")
        self.assertEqual(format_improvement(-6.25), "增加 **6.2%**")

    def test_validation_requires_a_complete_paired_grid(self) -> None:
        samples = [
            sample(round_number, mode, 1.0)
            for round_number in (1, 2)
            for mode in MODES
        ]
        audit = validate_sample_grid(
            samples,
            rounds=2,
            states=("plaintext-cold",),
            vm_counts=(1,),
            modes=MODES,
        )
        self.assertEqual(audit["sample_count"], 6)
        self.assertEqual(audit["response_sha256"], "same")
        self.assertTrue(audit["invariants"]["materialization_window_bounded"])

        with self.assertRaisesRegex(ValueError, "sample grid"):
            validate_sample_grid(
                samples[:-1],
                rounds=2,
                states=("plaintext-cold",),
                vm_counts=(1,),
                modes=MODES,
            )

    def test_validation_rejects_materialization_above_configured_window(self) -> None:
        samples = [sample(1, mode, 1.0) for mode in MODES]
        shared = next(
            item for item in samples if item["mode"] == "vhost-user-blk-shared-cache"
        )
        shared["counters"]["lazyd"]["measured"]["materialized_max_bytes"] = 2 << 20
        shared["counters"]["accelerator"]["measured"]["read_range_max_bytes"] = 2 << 20

        with self.assertRaisesRegex(ValueError, "materialization window"):
            validate_sample_grid(
                samples,
                rounds=1,
                states=("plaintext-cold",),
                vm_counts=(1,),
                modes=MODES,
                materialization_max_bytes=1 << 20,
            )

    def test_validation_rejects_lazyd_accelerator_max_mismatch(self) -> None:
        samples = [sample(1, mode, 1.0) for mode in MODES]
        shared = next(
            item for item in samples if item["mode"] == "vhost-user-blk-shared-cache"
        )
        shared["counters"]["accelerator"]["measured"]["read_range_max_bytes"] = 8192

        with self.assertRaisesRegex(ValueError, "maximum range bytes differ"):
            validate_sample_grid(
                samples,
                rounds=1,
                states=("plaintext-cold",),
                vm_counts=(1,),
                modes=MODES,
                materialization_max_bytes=1 << 20,
            )

    def test_validation_allows_transport_specific_cold_working_sets(self) -> None:
        samples = [sample(1, mode, 1.0) for mode in MODES]
        shared = next(
            item for item in samples if item["mode"] == "vhost-user-blk-shared-cache"
        )
        shared["counters"]["lazyd"]["measured"]["materialized_bytes"] = 8192
        shared["counters"]["accelerator"]["measured"]["read_range_bytes"] = 8192

        audit = validate_sample_grid(
            samples,
            rounds=1,
            states=("plaintext-cold",),
            vm_counts=(1,),
            modes=MODES,
            materialization_max_bytes=1 << 20,
        )

        self.assertEqual(
            audit["cold_materialized_bytes_by_mode"],
            {"vhost-user-blk-shared-cache": 8192, "lazy-pmem": 4096},
        )

    def test_paired_comparison_aligns_samples_by_round(self) -> None:
        values = {
            "vhost-user-blk": (10.0, 20.0),
            "vhost-user-blk-shared-cache": (8.0, 16.0),
            "lazy-pmem": (5.0, 10.0),
        }
        samples = [
            sample(round_number, mode, values[mode][round_number - 1])
            for round_number in (1, 2)
            for mode in MODES
        ]
        random.Random(7).shuffle(samples)
        rows = build_paired_comparisons(
            samples,
            metrics=(
                MetricSpec(
                    "application_ready_seconds",
                    "s",
                    lambda item: item["group"]["application_ready_seconds"],
                ),
            ),
            bootstrap_iterations=200,
        )
        pmem_vs_current = next(
            row for row in rows if row["comparison"] == "pmem_vs_current"
        )
        self.assertEqual(pmem_vs_current["rounds"], 2)
        self.assertEqual(pmem_vs_current["left_better_rounds"], 2)
        self.assertAlmostEqual(pmem_vs_current["improvement_pct_median"], 50.0)
        self.assertAlmostEqual(pmem_vs_current["paired_delta_median"], -7.5)

    def test_zero_baseline_keeps_absolute_delta_without_fake_percentage(self) -> None:
        samples = [sample(1, mode, 1.0) for mode in MODES]
        rows = build_paired_comparisons(
            samples,
            metrics=(MetricSpec("zero", "bytes", lambda _item: 0.0),),
            bootstrap_iterations=20,
        )
        self.assertTrue(all(row["paired_delta_median"] == 0 for row in rows))
        self.assertTrue(all(row["improvement_pct_median"] is None for row in rows))

    def test_line_chart_supports_single_vm_preflight_grid(self) -> None:
        rows = [
            {
                "source_state": state,
                "vm_count": 1,
                "mode": mode,
                "application_ready_seconds_median": 1.0,
                "application_ready_seconds_p95": 1.1,
            }
            for state in ("plaintext-cold", "plaintext-warm")
            for mode in MODES
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "ready.svg"
            svg_line_chart(
                rows,
                metric="application_ready_seconds",
                title="preflight",
                y_label="seconds",
                output=output,
                vm_counts=(1,),
            )
            contents = output.read_text(encoding="utf-8")
        self.assertIn("Concurrent VMs", contents)
        self.assertIn(">1</text>", contents)


if __name__ == "__main__":
    unittest.main()
