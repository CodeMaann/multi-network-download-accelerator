# Feasibility (day 0)

**Status: not yet measured.**

## What a no-go means

Written before any result exists: **a no-go kills only the real-world sanity check, not the
project.** All headline numbers come from the simulator, which does not depend on whether this
machine's links stack.

## Go/no-go criterion

Measure each interface's solo throughput (bound to its local IP), then all of them together.

- **Go:** combined ≥ 1.5 × the best single link.
- **No-go:** anything less. The usual cause is links that share one upstream (for example, home
  Wi-Fi and Ethernet off the same router).

## Measurements

`scripts/feasibility.py` does five jobs. Results are committed only from its `--redact` output.

| # | Measurement | Result |
|---|---|---|
| 1 | Solo and combined throughput per link, with verdict | — |
| 2 | Recorded traces (two sessions, am and pm) | — |
| 3 | Loopback CPU ceiling (Python + httpx) | — |
| 4 | NTFS zero-fill cost at a high offset in a fresh large file | — |
| 5 | `asyncio.sleep` accuracy vs the ~15.6 ms Windows timer | — |

## Verdict

—
