# Cache backing under cgroup memory pressure

Each sample runs 8 VMs, prewarms and scans the same 165.95 MiB EROFS tree, with `MemorySwapMax=0`.

| MemoryMax (MiB) | Transport | Backing | Passes | OOM | Runtime failures | Pass rate | Successful MemoryPeak p50 (MiB) |
|---:|---|---|---:|---:|---:|---:|---:|
| 2496 | Shared BLK | file | 3 | 0 | 0 | 100% | 2496.0 |
| 2496 | Shared BLK | memfd | 0 | 3 | 0 | 0% | - |
| 2528 | Shared BLK | file | 3 | 0 | 0 | 100% | 2503.1 |
| 2528 | Shared BLK | memfd | 3 | 0 | 0 | 100% | 2521.5 |
| 2560 | Shared BLK | file | 3 | 0 | 0 | 100% | 2515.7 |
| 2560 | Shared BLK | memfd | 3 | 0 | 0 | 100% | 2518.6 |
