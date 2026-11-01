# multi-network-download-accelerator

Downloads one file over Wi-Fi, Ethernet and mobile data at once via HTTP range requests.
Benchmarks three schedulers and decomposes where the unused bandwidth goes.

**Status: in progress.** Nothing here produces results yet.

## The idea

Each network interface gets its own connections, bound to that interface's local IP, and each
connection fetches byte ranges of the same file. The engineering question is how to split the work
when link speeds change mid-download. The research question is where the missing bandwidth goes:
for every run, the gap between the ideal time and the achieved time is split into scheduling,
tail, overhead, rate, duplicate and local losses, so it's clear which losses a scheduler can
actually remove.

Headline numbers will come from a deterministic virtual-time simulator, cross-validated against a
loopback HTTP tier, with a small real-world sanity check. The benchmark protocol will be committed
and tagged `protocol-v1` before any benchmark runs.

## Build plan

1. Virtual clock and event loop ✅
2. Feasibility script: do my links actually add up? (script ✅, measurements pending)
3. Link simulator
4. Block map and in-memory sink
5. Schedulers (static, static-oracle, work stealing, adaptive) and endgame
6. Event log and loss decomposition
7. Benchmarks (after the protocol is tagged)
8. Real downloader: interface binding, probe, disk writer, `download` command
9. Loopback tier and cross-validation
10. Write-up

The settled design is in [docs/01-design.md](docs/01-design.md).

## Development

```
git clone https://github.com/CodeMaann/multi-network-download-accelerator
cd multi-network-download-accelerator
uv sync --extra dev
uv run pytest -m "not network"
uv run ruff check . && uv run mypy
```

## License

MIT
