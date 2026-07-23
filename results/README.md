# Result archive

| Directory | Matrix | Samples |
|---|---|---:|
| `three-path` | 3 transports x cold/warm x 1/4/8 VM x 10 rounds | 180 |
| `cache-backing` | 2 transports x file/memfd x cold/warm x 1/4/8 VM x 10 rounds | 240 |
| `full-tree` | 2 transports x file/memfd x warm x 1/4/8 VM x 10 rounds | 120 |
| `pmem-pressure` | file/memfd x 5 limits x 5 rounds | 50 |
| `blk-pressure` | file/memfd x 3 limits x 3 rounds | 18 |

The functional result directories retain:

- `run-manifest.json`;
- `run-status.json`;
- raw per-sample JSON;
- analysis JSON/CSV/Markdown and figures.

The pressure directories retain:

- `run-manifest.json`;
- per-outcome classification JSON;
- complete worker results for successful samples;
- pressure summaries and figures;
- the worker/runner source snapshot.

Worker logs and VM binaries are intentionally omitted. Their hashes and source revisions remain
in the functional run manifests where the original runner recorded them.

