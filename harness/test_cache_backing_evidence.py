import unittest
from pathlib import Path

import run_cache_backing_evidence as evidence


class CacheBackingEvidenceTest(unittest.TestCase):
    def test_cell_orders_cover_each_transport_and_backing_once(self):
        expected = set(evidence.CELLS)
        for round_number in range(1, len(evidence.CELL_PERMUTATIONS) + 1):
            order = evidence.cell_order(round_number, 0)
            self.assertEqual(len(order), len(expected))
            self.assertEqual(set(order), expected)

    def test_worker_command_carries_cache_backing(self):
        command = evidence.worker_command(
            root=Path("/tmp/root"),
            output=Path("/tmp/result.json"),
            unit="test",
            round_number=1,
            execution_order=1,
            mode=evidence.MODE_PMEM,
            cache_backing="memfd",
            source_state="plaintext-cold",
            vm_count=1,
        )

        index = command.index("--cache-backing")
        self.assertEqual(command[index + 1], "memfd")


if __name__ == "__main__":
    unittest.main()
