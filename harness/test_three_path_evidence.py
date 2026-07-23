import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_three_path_evidence.py")


def load_module():
    spec = importlib.util.spec_from_file_location("three_path_evidence", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ThreePathEvidenceTest(unittest.TestCase):
    def test_mode_order_uses_all_six_permutations(self):
        runner = load_module()
        orders = {runner.mode_order(round_number, 0) for round_number in range(1, 7)}

        self.assertEqual(len(orders), 6)
        for order in orders:
            self.assertEqual(set(order), set(runner.MODES))

    def test_worker_command_uses_dedicated_accounted_cgroup(self):
        runner = load_module()
        command = runner.worker_command(
            root=Path("/bench"),
            output=Path("/out/sample.json"),
            unit="kuasar-evidence-r01-c1-b",
            round_number=1,
            execution_order=2,
            mode=runner.MODE_BLK,
            source_state="plaintext-cold",
            vm_count=4,
        )

        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--property=MemoryAccounting=yes", command)
        self.assertIn("--property=CPUAccounting=yes", command)
        self.assertIn("--property=IOAccounting=yes", command)
        self.assertEqual(command[command.index("--vm-count") + 1], "4")
        self.assertEqual(command[-2:], ["--cache-backing", runner.CACHE_BACKING])

    def test_completed_sample_requires_matching_contract(self):
        runner = load_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            result = runner.expected_result_contract(
                round_number=2,
                execution_order=3,
                mode=runner.MODE_PMEM,
                source_state="plaintext-warm",
                vm_count=2,
            )
            result.update(
                {
                    "schema_version": 1,
                    "cache_backing": runner.CACHE_BACKING,
                    "capture_order": list(runner.CAPTURE_ORDER),
                    "workload": {
                        "name": "nginx-first-request",
                        "result_kind": "sha256",
                        "response_sha256": "a" * 64,
                        "response_bytes": 1,
                    },
                    "vms": [{"vm_index": 1}, {"vm_index": 2}],
                    "counters": {
                        "accelerator": {},
                        "lazyd": {},
                        "cloud_hypervisor": {},
                        "vhost_root": {},
                    },
                }
            )
            path.write_text(runner.json.dumps(result), encoding="utf-8")

            self.assertTrue(runner.completed_sample(path, result))
            self.assertFalse(runner.completed_sample(path, {**result, "vm_count": 4}))


if __name__ == "__main__":
    unittest.main()
