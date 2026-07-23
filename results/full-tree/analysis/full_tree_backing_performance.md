# Lazy cache backing: full-tree evidence

Each VM reads 165.95 MiB from the same assembled EROFS tree. Every cell contains 10 paired rounds and the plaintext cache is materialized before the measured group.

## Cell medians

| VMs | Transport | Backing | App Ready p50/p95 (s) | Tree scan p50/p95 (s) | Steady-state cgroup memory delta p50 (MiB) | Operation CPU p50 (s) |
|---:|---|---|---:|---:|---:|---:|
| 1 | Shared BLK | file | 0.378/0.421 | 2.458/3.005 | 505.2 | 4.37 |
| 1 | Shared BLK | memfd | 0.382/0.399 | 2.629/2.912 | 499.4 | 4.61 |
| 1 | Lazy PMEM | file | 0.364/0.420 | 0.922/1.005 | 309.6 | 1.32 |
| 1 | Lazy PMEM | memfd | 0.358/0.409 | 0.936/0.998 | 305.2 | 1.32 |
| 4 | Shared BLK | file | 0.599/0.749 | 2.906/6.521 | 1366.0 | 19.31 |
| 4 | Shared BLK | memfd | 0.599/0.641 | 3.363/8.238 | 1359.6 | 22.05 |
| 4 | Lazy PMEM | file | 0.571/0.813 | 0.958/1.147 | 592.6 | 5.48 |
| 4 | Lazy PMEM | memfd | 0.578/4.179 | 0.954/1.076 | 584.4 | 5.46 |
| 8 | Shared BLK | file | 0.967/1.158 | 7.463/9.689 | 2510.4 | 70.02 |
| 8 | Shared BLK | memfd | 0.961/1.267 | 6.012/9.092 | 2511.6 | 64.50 |
| 8 | Lazy PMEM | file | 0.858/1.091 | 1.134/1.406 | 963.6 | 12.41 |
| 8 | Lazy PMEM | memfd | 0.849/1.133 | 1.191/2.730 | 955.7 | 12.77 |

## Memfd relative to file

Positive values mean memfd is lower. Win rate is based on paired rounds.

| VMs | Transport | Tree scan median / wins | Steady-state cgroup memory delta median / wins | Host writes median / wins |
|---:|---|---:|---:|---:|
| 1 | Shared BLK | -0.1% / 50% | +1.2% / 90% | +0.0% / 0% |
| 1 | Lazy PMEM | -2.5% / 30% | +1.6% / 90% | +0.0% / 0% |
| 4 | Shared BLK | -1.1% / 40% | +0.6% / 90% | +0.0% / 0% |
| 4 | Lazy PMEM | +0.0% / 50% | +0.7% / 80% | +0.0% / 10% |
| 8 | Shared BLK | +6.8% / 60% | +0.0% / 50% | +0.0% / 30% |
| 8 | Lazy PMEM | -0.5% / 40% | +0.8% / 80% | +0.0% / 0% |

## Lazy PMEM relative to shared-cache BLK

Positive values mean Lazy PMEM is lower under the same backing.

| VMs | Backing | Tree scan median / wins | Post-launch cgroup memory delta median / wins | Steady-state cgroup memory delta median / wins | CPU median / wins |
|---:|---|---:|---:|---:|---:|
| 1 | file | +63.2% / 100% | +68.1% / 100% | +38.5% / 100% | +70.4% / 100% |
| 1 | memfd | +64.1% / 100% | +68.4% / 100% | +39.0% / 100% | +70.9% / 100% |
| 4 | file | +68.9% / 100% | +67.6% / 100% | +56.6% / 100% | +73.0% / 100% |
| 4 | memfd | +71.4% / 100% | +67.6% / 100% | +57.0% / 100% | +75.0% / 100% |
| 8 | file | +84.0% / 100% | +67.5% / 100% | +61.6% / 100% | +81.9% / 100% |
| 8 | memfd | +76.7% / 100% | +67.6% / 100% | +61.9% / 100% | +77.9% / 100% |

## PMEM mapped-page sharing

| VMs | Backing | Mapped RSS/PSS p50 (MiB) | RSS/PSS | Cache identities |
|---:|---|---:|---:|---:|
| 1 | file | 174.0/174.0 | 1.00 | 1 |
| 1 | memfd | 174.0/174.0 | 1.00 | 1 |
| 4 | file | 696.1/174.0 | 4.00 | 1 |
| 4 | memfd | 696.1/174.0 | 4.00 | 1 |
| 8 | file | 1392.2/174.0 | 8.00 | 1 |
| 8 | memfd | 1392.2/174.0 | 8.00 | 1 |
