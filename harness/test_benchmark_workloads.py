import subprocess
import unittest

from benchmark_workloads import (
    GENERIC_MARKERS,
    build_workload,
    parse_workload_output,
    workload_names,
)


class BenchmarkWorkloadsTest(unittest.TestCase):
    def test_workload_names_distinguish_diagnostic_and_application_cases(self):
        self.assertEqual(
            workload_names(),
            ("full-tree-scan", "nginx-first-request", "mysql-capability-smoke"),
        )

    def test_full_tree_scan_uses_generic_markers_in_order(self):
        workload = build_workload("full-tree-scan", hold_seconds=7)

        self.assertEqual(workload.image_name, "openeuler-24.03-lts")
        self.assertEqual(workload.result_kind, "byte-count")
        self.assertIn("/opt/sandbox-runtime/bin/read-tree", workload.command)
        self.assertLess(
            workload.command.index(GENERIC_MARKERS["app_ready"]),
            workload.command.index(GENERIC_MARKERS["operation_begin"]),
        )
        self.assertLess(
            workload.command.index(GENERIC_MARKERS["operation_begin"]),
            workload.command.index(GENERIC_MARKERS["operation_end"]),
        )
        self.assertTrue(workload.command.endswith("sleep 7"))

    def test_nginx_workload_waits_for_service_and_hashes_measured_response(self):
        workload = build_workload("nginx-first-request", hold_seconds=11)

        self.assertEqual(workload.image_name, "nginx-1.27.3-alpine")
        self.assertEqual(workload.result_kind, "sha256")
        self.assertIn("nginx -g 'daemon off;'", workload.command)
        self.assertIn("http://127.0.0.1/", workload.command)
        self.assertIn(GENERIC_MARKERS["app_ready"], workload.command)
        self.assertIn("KUASAR_BENCH_RESULT_SHA256=", workload.command)
        self.assertIn("KUASAR_BENCH_BYTES=", workload.command)
        self.assertIn(
            f"echo {GENERIC_MARKERS['app_ready']}; sleep 2; "
            f"echo {GENERIC_MARKERS['operation_begin']}",
            workload.command,
        )
        self.assertTrue(workload.command.endswith("sleep 11"))

    def test_every_workload_is_valid_posix_shell(self):
        for name in workload_names():
            with self.subTest(name=name):
                workload = build_workload(name, hold_seconds=1)
                result = subprocess.run(
                    ["/bin/sh", "-n", "-c", workload.command],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_mysql_capability_smoke_does_not_claim_a_query_workload(self):
        workload = build_workload("mysql-capability-smoke", hold_seconds=1)

        self.assertEqual(workload.image_name, "mysql-8.4")
        self.assertEqual(workload.result_kind, "capability")
        self.assertIn("command -v mysqld", workload.command)
        self.assertIn("command -v mysqladmin", workload.command)
        self.assertNotIn("SELECT 1", workload.command)

    def test_unknown_workload_and_invalid_hold_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown workload"):
            build_workload("invented")
        with self.assertRaisesRegex(ValueError, "hold_seconds"):
            build_workload("full-tree-scan", hold_seconds=0)

    def test_parse_workload_output_validates_typed_values(self):
        digest = "a" * 64

        self.assertEqual(
            parse_workload_output(b"KUASAR_BENCH_APP_READY\n"), ("app_ready", None)
        )
        self.assertEqual(
            parse_workload_output(f"KUASAR_BENCH_RESULT_SHA256={digest}\n".encode()),
            ("result_sha256", digest),
        )
        self.assertEqual(
            parse_workload_output(b"KUASAR_BENCH_BYTES=4096\n"), ("data_bytes", 4096)
        )
        self.assertEqual(parse_workload_output(b"ordinary output\n"), (None, None))
        self.assertEqual(parse_workload_output(b"ordinary \xff output\n"), (None, None))
        with self.assertRaisesRegex(ValueError, "SHA256"):
            parse_workload_output(b"KUASAR_BENCH_RESULT_SHA256=bad\n")
        with self.assertRaisesRegex(ValueError, "byte count"):
            parse_workload_output(b"KUASAR_BENCH_BYTES=-1\n")


if __name__ == "__main__":
    unittest.main()
