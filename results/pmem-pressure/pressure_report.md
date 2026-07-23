# Cache backing under cgroup memory pressure

Each sample runs 8 VMs, prewarms and scans the same 165.95 MiB EROFS tree, with `MemorySwapMax=0`.

| MemoryMax (MiB) | Transport | Backing | Passes | OOM | Runtime failures | Pass rate | Successful MemoryPeak p50 (MiB) |
|---:|---|---|---:|---:|---:|---:|---:|
| 944 | Lazy PMEM | file | 5 | 0 | 0 | 100% | 944.0 |
| 944 | Lazy PMEM | memfd | 0 | 5 | 0 | 0% | - |
| 960 | Lazy PMEM | file | 5 | 0 | 0 | 100% | 960.0 |
| 960 | Lazy PMEM | memfd | 3 | 2 | 0 | 60% | 958.6 |
| 976 | Lazy PMEM | file | 5 | 0 | 0 | 100% | 967.1 |
| 976 | Lazy PMEM | memfd | 5 | 0 | 0 | 100% | 973.8 |
| 992 | Lazy PMEM | file | 5 | 0 | 0 | 100% | 984.9 |
| 992 | Lazy PMEM | memfd | 5 | 0 | 0 | 100% | 976.8 |
| 1024 | Lazy PMEM | file | 5 | 0 | 0 | 100% | 966.5 |
| 1024 | Lazy PMEM | memfd | 5 | 0 | 0 | 100% | 971.0 |
