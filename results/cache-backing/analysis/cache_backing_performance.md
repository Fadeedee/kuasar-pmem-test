# Lazy cache backing performance

All reported samples are retained. Each cell contains 10 paired rounds.

## Cell medians

| State | VMs | Transport | Backing | App Ready p50 / p95 (s) | First op p50 (ms) | Steady-state cgroup memory delta p50 (MiB) | CPU to ready p50 (ms) | Host writes p50 (MiB) |
|---|---:|---|---|---:|---:|---:|---:|---:|
| cold | 1 | Lazy PMEM | file | 0.785 / 1.128 | 3.089 | 115.8 | 639.6 | 10.6 |
| cold | 1 | Lazy PMEM | memfd | 0.509 / 0.716 | 2.914 | 114.4 | 632.3 | 0.2 |
| cold | 1 | Shared BLK | file | 0.782 / 0.967 | 3.115 | 124.5 | 730.5 | 11.3 |
| cold | 1 | Shared BLK | memfd | 0.615 / 0.666 | 3.048 | 123.9 | 690.4 | 0.2 |
| cold | 4 | Lazy PMEM | file | 0.969 / 1.270 | 3.730 | 310.9 | 1866.4 | 11.1 |
| cold | 4 | Lazy PMEM | memfd | 0.707 / 1.091 | 3.345 | 309.0 | 1811.6 | 0.7 |
| cold | 4 | Shared BLK | file | 0.968 / 1.157 | 3.474 | 340.5 | 2112.9 | 11.9 |
| cold | 4 | Shared BLK | memfd | 0.810 / 0.841 | 3.140 | 335.4 | 2046.5 | 0.7 |
| cold | 8 | Lazy PMEM | file | 1.161 / 2.277 | 3.304 | 565.5 | 3949.7 | 11.8 |
| cold | 8 | Lazy PMEM | memfd | 0.967 / 2.289 | 3.236 | 563.7 | 3797.7 | 1.5 |
| cold | 8 | Shared BLK | file | 1.340 / 1.493 | 3.798 | 627.5 | 4387.6 | 12.6 |
| cold | 8 | Shared BLK | memfd | 1.081 / 2.117 | 3.398 | 624.8 | 4271.6 | 1.5 |
| warm | 1 | Lazy PMEM | file | 0.445 / 0.470 | 2.989 | 115.5 | 358.2 | 0.2 |
| warm | 1 | Lazy PMEM | memfd | 0.445 / 0.455 | 2.977 | 114.8 | 355.1 | 0.2 |
| warm | 1 | Shared BLK | file | 0.461 / 0.515 | 3.192 | 123.5 | 392.0 | 0.2 |
| warm | 1 | Shared BLK | memfd | 0.465 / 0.492 | 3.255 | 121.9 | 398.7 | 0.2 |
| warm | 4 | Lazy PMEM | file | 0.641 / 0.889 | 3.085 | 307.0 | 1486.6 | 0.7 |
| warm | 4 | Lazy PMEM | memfd | 0.647 / 0.867 | 3.120 | 307.4 | 1481.9 | 0.7 |
| warm | 4 | Shared BLK | file | 0.681 / 1.068 | 3.585 | 335.1 | 1646.2 | 0.7 |
| warm | 4 | Shared BLK | memfd | 0.682 / 0.799 | 3.658 | 336.1 | 1651.9 | 0.7 |
| warm | 8 | Lazy PMEM | file | 0.944 / 2.908 | 3.394 | 562.3 | 3341.4 | 1.5 |
| warm | 8 | Lazy PMEM | memfd | 2.070 / 2.524 | 3.342 | 564.3 | 12464.1 | 1.5 |
| warm | 8 | Shared BLK | file | 1.263 / 2.573 | 3.650 | 617.9 | 4702.2 | 1.5 |
| warm | 8 | Shared BLK | memfd | 1.062 / 2.271 | 3.509 | 619.0 | 3762.8 | 1.5 |

## Memfd relative to file

Positive values mean memfd is lower; win rate is the fraction of paired rounds in which memfd is lower.

| State | VMs | Transport | App Ready median / wins | Steady-state cgroup memory delta median / wins | Host writes median / wins |
|---|---:|---|---:|---:|---:|
| cold | 1 | Lazy PMEM | +22.4% / 100% | +0.3% / 60% | +98.3% / 100% |
| cold | 1 | Shared BLK | +22.0% / 100% | +0.6% / 60% | +98.4% / 100% |
| cold | 4 | Lazy PMEM | +29.3% / 90% | +0.7% / 60% | +93.4% / 100% |
| cold | 4 | Shared BLK | +21.0% / 100% | +0.7% / 70% | +93.8% / 100% |
| cold | 8 | Lazy PMEM | +15.6% / 80% | +0.1% / 60% | +87.6% / 100% |
| cold | 8 | Shared BLK | +20.2% / 80% | +0.4% / 60% | +88.4% / 100% |
| warm | 1 | Lazy PMEM | -0.9% / 30% | +0.8% / 70% | +0.0% / 0% |
| warm | 1 | Shared BLK | -1.4% / 30% | +1.2% / 70% | +0.0% / 0% |
| warm | 4 | Lazy PMEM | -1.0% / 40% | +0.1% / 70% | +0.0% / 0% |
| warm | 4 | Shared BLK | +1.3% / 50% | -0.4% / 50% | +0.0% / 0% |
| warm | 8 | Lazy PMEM | -120.3% / 30% | -0.3% / 40% | +0.0% / 10% |
| warm | 8 | Shared BLK | +11.3% / 50% | -0.3% / 40% | +0.0% / 0% |

## Lazy PMEM relative to shared-cache BLK

Positive values mean Lazy PMEM is lower under the same backing and source state.

| State | VMs | Backing | App Ready median / wins | First op median / wins | Steady-state cgroup memory delta median / wins | Total PSS median / wins |
|---|---:|---|---:|---:|---:|---:|
| cold | 1 | file | +2.4% / 60% | -3.3% / 40% | +8.0% / 100% | +0.9% / 60% |
| cold | 1 | memfd | +17.3% / 70% | +6.8% / 60% | +8.2% / 100% | +1.6% / 60% |
| cold | 4 | file | +0.4% / 60% | -11.5% / 40% | +8.2% / 100% | +7.8% / 100% |
| cold | 4 | memfd | +11.7% / 70% | -2.2% / 40% | +8.0% / 100% | +6.7% / 100% |
| cold | 8 | file | +9.1% / 60% | +12.1% / 100% | +9.9% / 100% | +9.2% / 100% |
| cold | 8 | memfd | +11.0% / 80% | +5.9% / 70% | +9.9% / 100% | +9.5% / 100% |
| warm | 1 | file | +4.7% / 70% | +4.2% / 60% | +6.5% / 100% | +0.6% / 70% |
| warm | 1 | memfd | +4.7% / 100% | +5.0% / 70% | +5.9% / 100% | -0.6% / 30% |
| warm | 4 | file | +5.9% / 90% | +16.2% / 90% | +8.1% / 100% | +6.7% / 100% |
| warm | 4 | memfd | +4.8% / 80% | +15.0% / 80% | +8.9% / 100% | +7.0% / 100% |
| warm | 8 | file | +16.3% / 90% | +8.5% / 60% | +8.5% / 100% | +8.2% / 100% |
| warm | 8 | memfd | -1.0% / 50% | +4.4% / 70% | +8.7% / 100% | +8.4% / 100% |

## PMEM page-sharing evidence

Mapped RSS counts the same cache pages in every VMM; mapped PSS divides shared pages by the number of mappings. A ratio close to the VM count and one mapping identity indicate cross-VM sharing of one final cache object.

| State | VMs | Backing | PMEM mapped RSS / PSS p50 (MiB) | RSS/PSS | Cache identities | BLK root reads p50 (MiB) | Steady-state cgroup memory delta saved p50 (MiB) |
|---|---:|---|---:|---:|---:|---:|---:|
| cold | 1 | file | 6.5 / 6.5 | 1.00 | 1 | 6.3 | +9.9 |
| cold | 1 | memfd | 6.3 / 6.3 | 1.00 | 1 | 6.3 | +10.2 |
| cold | 4 | file | 26.1 / 6.5 | 4.00 | 1 | 25.0 | +27.8 |
| cold | 4 | memfd | 25.3 / 6.3 | 4.00 | 1 | 25.0 | +26.6 |
| cold | 8 | file | 52.2 / 6.5 | 8.00 | 1 | 50.1 | +62.4 |
| cold | 8 | memfd | 50.7 / 6.3 | 8.00 | 1 | 50.1 | +61.9 |
| warm | 1 | file | 6.5 / 6.5 | 1.00 | 1 | 6.3 | +8.0 |
| warm | 1 | memfd | 6.3 / 6.3 | 1.00 | 1 | 6.3 | +7.2 |
| warm | 4 | file | 26.1 / 6.5 | 4.00 | 1 | 25.0 | +27.2 |
| warm | 4 | memfd | 25.3 / 6.3 | 4.00 | 1 | 25.0 | +29.9 |
| warm | 8 | file | 52.2 / 6.5 | 8.00 | 1 | 50.1 | +52.4 |
| warm | 8 | memfd | 50.7 / 6.3 | 8.00 | 1 | 50.1 | +53.7 |
