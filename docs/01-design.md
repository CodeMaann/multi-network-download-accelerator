# Design

This document is authoritative. If code, a later doc, or a conversation disagrees with it, this
document wins until it is changed here, in a commit that says why. Decisions were settled on
2026-11-01, before any benchmark code existed.

## Architecture rules

These three rules are repeated at the start of every build step.

1. **`sched/` and `estimate/` import nothing from httpx, psutil, sockets, or real time.** Schedulers
   are pure decision functions (state in, assignment out). They are unit-tested against `FakeLink`
   with an injected clock.
2. **Only a `BlockSink` implementation touches storage.** `mem_sink` for the virtual tier,
   `disk_writer` for real downloads. Nothing else opens the output file.
3. **Every metric is derived offline from the JSONL event log.** Nothing is computed inline.

## 1. The question

A Windows CLI downloads one file over several network interfaces at once (Ethernet, Wi-Fi,
USB-tethered mobile data), using HTTP Range requests, with each connection bound to one
interface's local IP.

The centrepiece result is a **loss decomposition**: for each run, where did the gap between the
ideal download time and the achieved one go, and which of those losses can a scheduler actually
remove? The secondary question is how much an adaptive scheduler beats static split and work
stealing when link speeds change. That margin will probably be a few percent and cannot be
controlled; the decomposition is informative whatever it turns out to be.

## 2. Experimental design

### Baseline

The headline baseline is the **single best link with the same total connection count** as the
multi-link run. Multi-link uses 3 links × 3 connections = 9, so the baseline is 1 link × 9
connections. A 1-connection baseline would partly measure "more sockets", which speeds up a single
interface on its own (per-connection rate limits, congestion window, server-side fairness).

"Best link" means the link with the highest mean capacity over the run, which matters when
capacities change mid-run.

### Connections per link

K = 3 per link for the whole main matrix. Connection count is a nuisance parameter, not a research
variable. One separate sweep over K ∈ {1, 2, 3, 6} in a single scenario justifies the choice and is
reported in an appendix; it is not crossed into the matrix.

### The matrix

| Scheduler | Endgame off | Endgame on |
|---|---|---|
| static split (equal shares) | ✓ | ✓ |
| work stealing (chunk size swept) | ✓ | ✓ |
| adaptive chunk sizing | ✓ | ✓ |
| **static-oracle** (control, SIMULATOR ONLY) | ✓ | ✓ |

- **Endgame is a crossed factor**, not part of "adaptive". Bundling it into adaptive would make
  adaptive look better against a work stealer that was denied the same tool.
- **Static split** uses equal shares, because estimators start cold and nothing is known up front.
- **Static-oracle** splits in proportion to each link's true mean capacity. True mean capacity only
  exists in simulation, so this control is labelled SIMULATOR ONLY in every table. It answers "does
  adapting beat merely knowing the averages?"
- **Chunk size is confounded with scheduler choice.** Work stealing's chunk size is swept over
  {1, 2, 4, 8, 16} MB and adaptive is compared against work stealing's **best** value. No wins over
  a strawman.
- **Hyperparameters** (EWMA α, target chunk duration, endgame trigger) are tuned on held-out seeds
  and traces, never on the reported scenarios.

### Pairing and statistics

All schedulers in a comparison face an **identical environment**. Capacity, RTT, jitter and stall
traces are generated ahead of time as functions of time, from a seed, and are not drawn per
request. (A per-request draw would give schedulers that make different numbers of requests
different networks, which breaks both the pairing and the decomposition.) Comparisons use a paired
bootstrap over seeds.

### Metrics

- **Efficiency** (achieved ÷ sum of solo speeds) is reported for simulated runs only. In the real
  world the denominator is measured at a different time than the numerator, and mobile data drifts
  by the minute.
- **Real-world runs** report speedup against the connection-matched baseline, with solo speeds
  measured immediately before and after each run and reported as a range.
- **Tail time** is defined on useful bytes written to the output, not bytes received. Endgame
  duplicates inflate bytes received, which would move the 95% mark and make the metric
  incomparable across schedulers.

### Pre-registration

`docs/03-benchmark-protocol.md` is written and committed **alone** before any benchmark runs, and
tagged `protocol-v1`. It includes the cross-validation contradiction criterion (section 5) and the
tolerance for the decomposition audit (section 3). If the protocol changes after the tag, the change
is committed and tagged `protocol-v2`, and the README says what changed and why. Tags are never
moved or rewritten.

## 3. Loss decomposition

**This is a SIMULATOR-ONLY result.** It needs the ground-truth capacity rᵢ(t), which does not exist
in a real run.

### Capacity

The effective capacity of link i is

    rᵢ(t) = min(link_capᵢ(t), Kᵢ(t) · conn_capᵢ)

so capacity that the chosen connection count cannot reach is not charged as a loss. Kᵢ(t) can change
during a run, because an endgame duplicate occupies a connection. Capacity is **0 during stalls and
disconnects**: the network is not charged to the scheduler. The `capacity_change` event carries the
effective value, the raw link cap, and K.

### Unused capacity

For a download of B bytes finishing at time T:

    U = ∫₀ᵀ Σᵢ rᵢ(t) dt − B

The ideal time is T* such that ∫₀^T* Σᵢ rᵢ(t) dt = B. (`B / Σ rᵢ` is only correct when capacities are
constant, which is exactly the case this project is not about.)

### The six buckets

U is split into six buckets, in bytes:

| Bucket | Meaning |
|---|---|
| **scheduling** | a link sat idle while it still had useful work ahead of it |
| **tail** | a link sat idle after its last useful byte |
| **overhead** | requests were outstanding but no first byte had arrived yet (RTT, headers, handshakes) |
| **rate** | bytes were flowing below capacity (slow-start ramp, jitter, too few connections) |
| **duplicate** | bytes were received but discarded because the block was already complete |
| **local** | the writer was applying backpressure (expected ≈ 0; reported to show that it is) |

**Attribution.** At each instant, let uᵢ(t) be the rate of useful bytes and dᵢ(t) the rate of
duplicate bytes on link i. Duplicate loss is dᵢ(t), counted directly. The **residual**
rᵢ(t) − uᵢ(t) − dᵢ(t) goes whole to exactly one bucket, chosen from the states of the link's
connections by priority:

    local > idle > overhead > rate

That is, if any connection on the link is writer-blocked, the residual is local; otherwise, if any
connection has no outstanding request, it is idle; otherwise, if any connection is waiting for its
first byte, it is overhead; otherwise it is rate. Residuals are not clamped. Negative residuals
(more bytes in an interval than the capacity allowed, from binning) are summed as they are, and the
audit reports their total.

**Idle splits into scheduling and tail at a per-link boundary**: the time of that link's last
useful byte. Idle time before the boundary is scheduling loss; idle time after it is tail loss.

- A link that is alive but delivers **zero useful bytes** in the whole run (an unused link under
  static split, or a link that only ever delivered duplicates) has its boundary at t = 0, so all its
  idle capacity is tail loss.
- A link whose capacity is 0 throughout (disconnected) contributes nothing to any bucket.

This boundary does not depend on which scheduler ran. (An earlier definition, "the instant the work
queue is exhausted", fires at t = 0 under static split and would have charged all of static's idle
time to tail. It was rejected for that reason.)

Expect scheduling loss near zero for work stealing and adaptive, because both hand out work the
moment a connection asks for it. That is a finding, not a defect.

### Self-audit

On **every** benchmark run, the six buckets must sum to U within the tolerance stated in the
protocol. A run whose buckets don't close is **invalid** and is discarded loudly, never rescaled.
Because the connection states partition each link's timeline, the sum is an identity when the
bookkeeping is right. The audit therefore catches bookkeeping bugs (gaps or overlaps between states,
lost events, useful bytes ≠ B) but not bytes landing in the wrong bucket. Misattribution is caught
by unit tests on synthetic event logs with hand-computed answers.

## 4. Components

### Clock (`clock.py`)

`Clock` Protocol with `now()` and `sleep()`. `RealClock` uses monotonic time. `VirtualClock` runs
code on a `VirtualEventLoop` that jumps to the next timer instead of waiting, so the same program
produces the same timeline every time and runs hundreds of times faster than real time. Strict mode
raises `VirtualTimeViolation` on `time.monotonic` / `perf_counter` / `sleep`, threads, or
executors, and `VirtualDeadlock` instead of hanging. Every event timestamp comes from the run's
clock (`loop.time()`), never from wall-clock time.

The virtual loop relies on asyncio internals (`_selector`, `_clock_resolution`, the shape of
`_run_once`), which is why CI runs the clock tests on Python 3.11, 3.12 and 3.13. The stock
`_clock_resolution` is the monotonic clock's resolution, 15.6 ms on Windows, which would make
virtual timers fire early; the virtual loop overrides it to 1 ns.

### Link Protocol (`net/link.py`, `sim/simlink.py`)

    name: str
    local_ip: str
    fetch(byte_range) -> AsyncIterator[bytes]     # pieces of ~256 KB

- **Streaming**, not `-> bytes`. Whole-chunk fetches would hold too much in memory (12 connections ×
  16 MB plus duplicates), give the estimator one sample per chunk, and throw away a cancelled copy's
  bytes.
- Cancellation is part of the Protocol: closing the iterator stops the transfer.
- Typed errors: `LinkDown`, `Stalled`, `RangeUnsupported`, `ValidatorChanged`.
- Implementations: `HttpxLink` (real), `SimLink` (virtual tier), `FakeLink` (tests).
- Schedulers import nothing from httpx or psutil. CI never has three interfaces, so every scheduler
  test runs with no sockets and an injected clock.

### Blocks and the sink (`core/blocks.py`, `core/sink.py`, `core/mem_sink.py`, `core/disk_writer.py`)

- The file is divided into **1 MB blocks**. A chunk is always a whole number of blocks.
- Each connection collects whole blocks (at most 1 MB buffered per connection) and hands only
  complete blocks to the sink.
- **Block writes are idempotent: the first copy of a block wins.** A block that is already complete
  is discarded and counted as duplicate loss. This removes any winner/loser logic from endgame and
  makes duplicate accounting exact.
- The sink's queue is **bounded by bytes**, not items, which gives backpressure.
- "Block complete" means the `write()` call returned in the writer thread. There is no per-block
  fsync; there is one fsync at the end, before the SHA-256 check.
- `mem_sink` (virtual tier): in memory, no thread, no disk. Threads and real disk I/O cannot run in
  virtual time.
- `disk_writer` (real tier): one file handle, owned by one writer, writing in a thread. **Why a
  single writer:** in single-threaded asyncio, `seek()` + `write()` with no `await` between them
  cannot be interrupted, so the real problem is that blocking disk I/O stalls the event loop and
  contaminates throughput measurement. That forces writes into a thread, and once writes are in a
  thread, concurrent `seek()` + `write()` on a shared handle does race (`os.pwrite` does not exist
  on Windows). One writer thread owning one handle removes the race.
- The writer emits `writer_blocked` events. The estimator excludes writer-blocked intervals, so a
  slow disk cannot masquerade as a slow link in the estimator or in the decomposition.
- NTFS zero-fill: writing far past the end of a fresh file makes NTFS fill the gap with zeros. Its
  cost is measured in the feasibility script; sparse files (`FSCTL_SET_SPARSE` via ctypes) are used
  only if the measurement says it matters.

### Schedulers (`sched/`)

- `static_split`: equal shares, plus the static-oracle variant (simulator only).
- `work_stealing`: fixed chunk size from config, swept.
- `adaptive`: chunk size from estimated link rate × target chunk duration, rounded to whole blocks.
  **At risk**: the first thing cut if the core overruns.
- `endgame`, combinable with any scheduler:
  - Trigger: duplicate onto an idle link when its estimated time to finish the leader's remaining
    blocks beats the leader's own estimated finish time. Because the trigger uses the estimator, the
    estimator survives even if adaptive sizing is cut.
  - The duplicate starts at the leader's current block boundary, not at the start of the chunk.
  - A copy is cancelled as soon as its next block is already complete (it has been overtaken).
  - When every block of the chunk is complete, all copies are cancelled.

### Estimator (`estimate/`)

- **EWMA is the primary estimator.** It estimates each link's total rate from windowed byte counts
  across all of that link's connections, not from per-chunk durations. (Connections on one link
  share a bottleneck, and short chunks include the RTT.)
- Estimators start cold. psutil's interface speed is the NIC's negotiated link rate (it reports
  1000 Mbps on a 50 Mbps connection), so it is for display only and never reaches an estimator or
  scheduler.
- The estimator comparison is EWMA vs sliding-window median, offline, on the recorded traces, for
  bias and variance under regime change. A 1-D Kalman filter is out unless a measurement says
  otherwise.
- **No bandits or Thompson sampling.** Those solve explore/exploit, where you pick one arm and
  don't observe the others. Here every link is used at once and every link reports a fresh
  measurement, so there is nothing to explore.

### HTTP (`net/`)

- HTTP/1.1 is forced, and the negotiated protocol is asserted. (Under HTTP/2, httpx multiplexes
  requests over one connection and connection counts lose their meaning.)
- Keep-alive connections are reused within each link.
- `Accept-Encoding: identity` on every request. (httpx asks for gzip by default, and byte ranges of
  a compressed response don't match the file.)
- **Every response is checked for 206 with a matching `Content-Range`.** A server that ignores
  Range sends 200 and the whole file down every connection.
- **DNS is resolved once, the A record is pinned, IPv4 only, and the Host header is preserved.**
  This is an experimental-validity issue, not just a bug: if every link resolves through the
  default interface, they all hit the same CDN edge and may share a server-side limit, which would
  look like a scheduler failure.
- `probe.py` reads size, `Accept-Ranges`, strong vs weak ETag, and Last-Modified.

### Event log (`telemetry/events.py`)

JSONL, one record per event, per **connection**, timestamped from the run's clock. Every scheduling
decision and every transfer state change is logged, so how long each connection and link sat idle
can be reconstructed offline. Event types (the schema is finalised in step 6):

`run_start` (config, seed), `request_sent`, `first_byte`, `progress`, `chunk_complete`,
`chunk_cancelled`, `block_complete`, `write_done`, `no_work`, `writer_blocked`, `link_error`,
`capacity_change` (simulator ground truth: effective capacity, raw link cap, K).

### Deferred features

- **Resume.** State is a bitmap over the 1 MB blocks (completed regions are not contiguous under work
  stealing). The validator is a strong ETag, falling back to Last-Modified + Content-Length; with
  neither, resume is refused rather than guessed. Resumed requests use `If-Range`; weak ETags (`W/`)
  cannot be used with it. The state file is written atomically (temp file + `os.replace`), retrying
  on `PermissionError` when antivirus or the search indexer holds the file open.
- **Per-link data caps.** Enforced before a chunk is dispatched, with the chunk sized so the cap
  cannot be overshot. The whole chunk is reserved at dispatch and settled afterwards. Endgame
  duplicates and bytes that arrive after a cancel count against the cap.

## 5. Simulator

### Two tiers

- **Virtual-time tier** (primary; produces the headline numbers). `SimLink` on the virtual clock,
  with environment traces generated ahead of time from a seed.
- **Loopback HTTP tier** (validation). A range-supporting HTTP server in a **separate process**
  that shapes traffic, using plain HTTP (TLS on loopback would measure the CPU, not a network path).
  Cut down to 2 scenarios × 2 schedulers.

**The tiers must be cross-validated.** The same seeded scenarios are run in both, and the
discrepancy is reported as a result. Runs cannot be identical across tiers (request timing
differs), so the comparison is between effect sizes over N seeds. The protocol pre-registers what
counts as a contradiction (for example, the scheduler ranking flipping). Without this, the
virtual-time tier would be unfalsifiable.

### What each link models

Bandwidth cap, per-connection rate cap, base RTT, RTT jitter, stall events, hard disconnect, and
slow start as a per-connection rate ramp. A bandwidth-only simulator (~0 ms RTT, no jitter, no
stalls) would make every scheduler look good, shrink the tail to nothing, make endgame look useless,
and disagree with real runs for reasons that can't be diagnosed. Mobile data's defining trait is
high, variable RTT with deep buffers, not low bandwidth.

### What neither tier models

Real TCP congestion windows and loss recovery. Windows has no netem, and loopback TCP is not a
network path. The README says so.

### Scenarios

`stable`, `drifting`, `sudden-drop`, `disconnect`, `adversarial` (bursty, heavy-tailed jitter,
correlated drops across links), plus recorded traces.

Synthetic traces are deliberately not smooth. If the simulator only produced gentle piecewise
curves and EWMA were tuned to track gentle curves, the scheduler would be tested against the model
it assumes, and adaptive would win for reasons that say nothing about real networks.

### Recorded traces

- Two sessions at different times of day (mid-morning and evening peak), about 750 MB of mobile
  data each.
- About 6 connections per link to saturate it, with all links recorded at the same time, so
  correlated drops are real.
- 100 ms bins. A 1-second average would smooth away the burstiness that matters.
- At ~50 Mbps, 750 MB lasts about 2 minutes. Short traces are not looped to make them longer;
  looping would add an artificial repeating pattern.
- A recording measures **goodput, replayed as capacity**. That wording appears in the README and in
  the metric definitions, along with "two samples of my own real conditions".

Trace format, fixed: CSV `t_ms,link,bytes`, one row per 100 ms bin per link.

- `t_ms`: integer, monotonic, starts at 0, bin width exactly 100.
- `link`: stable string id (for example `mobile0`), the same ids as the scenario YAML.
- `bytes`: integer bytes delivered to the application in that bin.
- Every link has a row for every bin, including zeros. Rows are never sparse: the replayer must not
  have to guess whether a gap is a stall or a missing sample.

Sidecar `<name>.meta.json`: `recorded_at` (ISO 8601, local time zone), `duration_s`,
`connections_per_link`, links (id, interface name, carrier/ISP label, whether mobile),
`server_url`, `total_bytes_per_link`, `sample_ms`, recorder git SHA, and a free-text `conditions`
note.

## 6. Real-world sanity check

A sanity check, not a results table. All headline numbers come from the simulator, and the README
says so.

- One scenario, a 300-500 MB file, against the author's own VPS (not a volunteer mirror), with the
  per-IP connection cap set explicitly on the server.
- Three configurations: the connection-matched baseline (1 link × 9), work stealing at its best
  chunk size, and the best adaptive + endgame configuration.
- 5 paired runs in alternating order (ABC / CBA), with solo per-link speed tests logged immediately
  before and after each run.
- HTTP is the primary protocol, plus one HTTPS run to measure per-connection handshake cost as part
  of overhead loss. The HTTPS run is the first thing cut if it gets fiddly with pinned IPs.
- Mobile data budget: 4 GB total, 1.5 GB for trace recording and 2.5 GB for real-world runs. The
  recorder and the runner take `--budget-mb` and stop hard on it; the budget applies to the mobile
  link only.

## 7. Feasibility (day 0)

`scripts/feasibility.py` is a disposable script that does five jobs, with results in
`docs/00-feasibility.md`:

1. Solo and combined throughput per link (bound to its local IP), with the go/no-go verdict:
   combined ≥ 1.5 × the best single link.
2. The trace recorder.
3. The loopback CPU ceiling (can Python + httpx saturate the speeds the simulator will use?).
4. NTFS zero-fill cost when writing at a high offset in a fresh large file.
5. `asyncio.sleep` accuracy against the ~15.6 ms Windows timer (this affects the loopback tier's RTT
   emulation).

It takes `--redact`, which writes a committable copy: public IP removed, carrier and ISP reduced to
generic labels ("mobile carrier A", "home ISP"). Private addresses (192.168.x.x, 10.x.x.x) are kept.

**A no-go kills only the real-world sanity check, not the project.**

### Windows source-IP binding

Windows uses the strong host model by default: a packet sent from IP X leaves through the interface
that owns X, and route lookup only considers that interface's routes. Interface metric picks the
interface for unbound traffic and shouldn't break bound sockets. These do:

- No default gateway on the interface (common with USB tethering; also a `169.254.x.x` address from
  failed DHCP).
- Wi-Fi soft-disconnect when Ethernet is up: the "Minimize the number of simultaneous connections"
  policy (`fMinimizeConnections`).
- `weakhostsend=enabled` on an interface: packets leave through the wrong interface and the upstream
  NAT drops them.
- VPN clients that install `0.0.0.0/1` and `128.0.0.0/1` routes or force all traffic through the
  tunnel.
- DNS is not bound to the interface (see HTTP above).

If a link reads 0 MB/s, try `curl.exe --interface <local_ip> -o NUL <url>` first: if curl fails too,
the problem is the OS, not the code. Then:

1. In `route print`, check that the link's IP is in the Interface column of a `0.0.0.0  0.0.0.0` line
   with a real gateway.
2. Look for VPN routes that override it.
3. Confirm the address isn't `169.254.*`.
4. Cross-check with `Get-NetRoute -DestinationPrefix 0.0.0.0/0` and `Get-NetIPInterface`.

## 8. Scope and budget

The budget is 30-36 hours (10-12 h/week × 3 weeks). This is a side project, so the scope is
deliberately narrow: a narrow, correct result beats a wide, unfinished one.

### Build order

| Step | Work | Hours |
|---|---|---|
| 1 | `clock.py` + the virtual event loop (hard 3 h timebox) | 3 |
| 2 | `scripts/feasibility.py` (the five jobs) | 3 |
| 3 | `sim/`: SimLink, pre-generated environment traces, profiles, replay | 3 |
| 4 | `core/blocks.py`, `sink.py`, `mem_sink.py` | 2 |
| 5 | `sched/` (static + oracle, work stealing, endgame) + `estimate/ewma.py`; `adaptive.py` last | 5 |
| 6 | `telemetry/events.py` + `decompose.py`, including the sum audit | 4 |
| 7 | `benchmarks/run_bench.py` + `analyze.py` + the chunk-size sweep | 3 |
| 8 | `net/` (resolve, probe, HttpxLink) + `core/disk_writer.py` + the `download` command | 4 |
| 9 | `loopback/` + `crossval.py` (2 scenarios × 2 schedulers) | ~6 |
| 10 | README + docs | 3 |

`docs/03-benchmark-protocol.md` is committed alone and tagged `protocol-v1` before step 7 runs.

- **Must ship (MVP):** steps 1-8 and 10. The real downloader is in the MVP: without it the repo would
  be a simulator with a download-accelerator title.
- **Should ship (validation):** step 9.
- **Nice to have:** the real-world sanity check (it dies on a feasibility no-go).

### Cut order

If time runs out, cut in this order:

1. resume
2. per-link data caps
3. adaptive chunk sizing
4. endgame, last, and only if everything is on fire. It is cheap, targets tail loss directly, and
   is the best story.

Out unless a measurement says otherwise: sparse-file engineering, the Kalman estimator, the HTTPS
comparison run.

## 9. Repository policy

### Commits

Work goes directly on `main`, one commit per build step, named after the step ("step 1: virtual
clock and event loop", "step 2: feasibility script", …). The history reads as the build order. Empty
placeholder modules are never committed; each step adds its real modules.

### Data

- **Commit:** aggregated results (`benchmarks/results/*.csv`), figures (`benchmarks/figures/*.png`),
  scenarios (`scenarios/*.yaml`), recorded traces (`scenarios/recorded/*.csv` + `*.meta.json`), docs,
  and the lockfile.
- **Ignore:** raw per-connection JSONL event logs (`benchmarks/results/raw/`). They are large and can
  be regenerated, so the seed and config for every reported run are committed. If a specific log is
  needed as evidence, that one log is committed gzipped.
- **Never:** downloaded test files, the venv, anything containing a key.

### Privacy

Feasibility output and trace metadata pass through `--redact` before being committed: no public IP,
and generic carrier/ISP labels.

### CI

No network. Tests that touch a real interface or external host are marked `@pytest.mark.network`
and deselected. CI runs ruff, strict mypy, unit tests on Windows with Python 3.11/3.12/3.13, and
loopback integration tests on the pinned version (3.12) once they exist.

## 10. Planned layout

This is the plan, not the current state. Modules appear in the build step that implements them.

```
src/mnda/
  clock.py            Clock Protocol, RealClock, VirtualClock            (step 1)
  cli.py              Typer: download, interfaces, bench, trace
  config.py           chunk/block sizes, K, caps, budgets
  errors.py           LinkDown, Stalled, RangeUnsupported, ValidatorChanged
  net/                interfaces (psutil, display only), resolve, probe, link (HttpxLink)
  core/               blocks, plan, sink, mem_sink, disk_writer, verify, state (deferred), engine
  sched/              base (pure decision Protocol), static_split (+oracle), work_stealing,
                      adaptive (at risk), endgame
  estimate/           base, ewma, window (sliding median)
  telemetry/          events, meter (excludes writer-blocked intervals), decompose, ui (Rich)
  sim/                simlink, env (pre-generated traces), profiles (YAML), replay (recorded CSV)
  loopback/           server, shaper (separate process)
scripts/feasibility.py
scenarios/            *.yaml, recorded/*.csv + *.meta.json
benchmarks/           run_bench.py, crossval.py, analyze.py, results/, figures/
tests/                fakes/, unit/, integration/
docs/                 00-feasibility, 01-design, 02-scheduler-notes, 03-benchmark-protocol, adr/
```
