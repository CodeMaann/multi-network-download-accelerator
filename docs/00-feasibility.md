# Feasibility (day 0)

**Status: verdict recorded (NO-GO on the real-world tier). Trace sessions pending.**

## What a no-go means

Written before any result exists: **a no-go kills only the real-world sanity check, not the
project.** All headline numbers come from the simulator, which does not depend on whether this
machine's links stack.

## Go/no-go criterion

Measure each interface's solo throughput (bound to its local IP), then all of them together, then
each solo again.

- **Go:** combined ≥ 1.5 × the best single link (the mean of its before and after solo runs).
- **No-go:** anything less, or fewer than two live links. A link under 1 Mbps on its own is
  effectively dead and excluded from the stacking claim.
- **Suspect:** a no-go where every link reached the same server IP. A limit on that server's side
  can't be ruled out, so the run is repeated against a second target before the no-go stands.
- **Invalid:** some link's connections did not leave from its own local IP; or, on a live link, a
  phase measured 0.0 Mbps, solo before and after differ by more than 1.5×, the server refused every
  connection for part of a phase, or combined exceeds the sum of the solo speeds.

The script also reports stacking efficiency (combined ÷ sum of solo speeds). High efficiency with
a no-go means the links stack but the others are slow compared with the best one. Low efficiency
means they interfere. The full precedence is in [01-design.md, section 7](01-design.md#7-feasibility-day-0).

## Before running

Each of these can produce a false no-go:

1. **Phone Wi-Fi off** before turning on USB tethering. Otherwise the phone can tether through your
   home Wi-Fi, and the "mobile" link becomes a second path to the same router.
2. **Mobile data on, USB tethering on**, and the phone's data saver and battery saver off. Some
   plans throttle tethered traffic specifically; if the mobile link is much slower tethered than on
   the phone itself, that is why.
3. **VPN off**, including always-on clients such as Cloudflare WARP, Tailscale exit nodes or
   corporate VPNs. The script warns if it sees VPN-style split routes.
4. **Ethernet plugged in** if you're using it, and Wi-Fi still connected afterwards. If Wi-Fi drops
   the moment Ethernet connects, Windows' `fMinimizeConnections` policy is soft-disconnecting it
   (the script reports this policy).
5. **Pause other traffic:** OneDrive or Dropbox sync, Windows Update (and Delivery Optimization),
   game launchers, streaming.
6. Expect Wi-Fi and Ethernet from the **same router** to share one upstream. The script warns when
   two links use the same gateway. Realistically, the gain comes from the mobile link.
7. **`--conns 2`.** Public speed-test mirrors cap concurrent connections per client IP (Hetzner: 2)
   and answer the rest with HTTP 429. Extra connections measure nothing and, retried too eagerly,
   get the machine locked out (OVH). The script now backs off and reports the effective count.
8. **For a 20-minute trace:** laptop on mains power, lid open. The recorder stops Windows sleeping
   on idle, but closing the lid or choosing Sleep still suspends it.

## How to run

From the repo root, in the project venv. Raw output goes to `feasibility-out/` (git-ignored).

```
# 0. See your adapters and the suggested --link flags; fix any warnings first.
.venv\Scripts\python scripts\feasibility.py interfaces

# 1. Throughput and verdict.
.venv\Scripts\python scripts\feasibility.py throughput --link wifi0="Wi-Fi" --link mobile0="Ethernet 3" --mobile mobile0 --isp mobile0="<carrier>" --isp wifi0="<home ISP>" --conns 2 --budget-mb 300

# 2. Trace sessions, mid-morning and evening peak: 20 min each, continuous (the default mode).
.venv\Scripts\python scripts\feasibility.py trace --session am <same --link/--mobile/--isp flags> --conns 2 --budget-mb 750 --conditions "..."
.venv\Scripts\python scripts\feasibility.py trace --session pm <same flags> --conns 2 --budget-mb 750 --conditions "..."

# 3-5. No network needed. Point zerofill at the drive you'll download to.
.venv\Scripts\python scripts\feasibility.py loopback
.venv\Scripts\python scripts\feasibility.py zerofill --dir D:\mnda-zerofill
.venv\Scripts\python scripts\feasibility.py timer

# Committable copies: docs/feasibility/ and scenarios/recorded/. Read them before `git add`.
.venv\Scripts\python scripts\feasibility.py redact
```

Mobile data used by every run accumulates in `feasibility-out/mobile-ledger.jsonl`.

## How to read the trace

**One cold start.** A continuous session opens its connections once, at t = 0. The first 10 s
(`--warmup-s`) carry bytes with `saturated = 0`; every later bin is saturated. A connection makes a
new request only when its 1 GB body ends or after an error, so `connection_reuse` (requests vs
distinct local ports) counts reconnects, and `error_counts` says why. (The older duty-cycled mode
is still available as `--mode duty-cycled`; it closes every connection at the end of each cycle,
so each cycle is a cold start.)

**Effective connections.** `effective_connections_mean` is connection-seconds with a body
streaming, divided by the session length: how many connections were really downloading. If it is
below `--conns`, or `rejected_by_server` is non-zero, the server capped the connections and the
job says so.

**Measured zero vs missing.** In a saturated bin, `bytes = 0` is real data: every connection was
trying and the link delivered nothing. An **empty** `bytes` field means the recorder could not
measure the link in that bin: every one of its connections was in a measurement fault (an HTTP
error status such as a server refusal, a DNS failure, a refused connection), or the recorder itself
stalled. Timeouts, resets and "network unreachable" count as the link failing, so they stay zeros.
`dropouts` in the meta file counts zero-byte bins and outages (zero runs of 1 s or more). Below
about 1 Mbps a bin holds a few packets, so isolated zero bins there are packet timing, not
outages.

**The warm-up check.** For each link, the job compares the first saturated 100 ms bin with the rest
of the window (`warmup_check`). It only claims a ratio when one can mean something:

- `too_slow`: the link averaged under 1 Mbps, where one packet moves a bin by more than the 15%
  being tested. Not checked.
- `inconclusive`: bin-to-bin noise is too large for the number of windows (the standard error of
  the first-bin ratio exceeds 0.075, half the 15% margin). With one continuous window this is the
  expected result on a variable link. It matters little here: the warm-up is 10 s of a 20-minute
  session, and its bins are in the CSV for inspection.
- `insufficient`: the first bin is below 0.85 of the rest (or low even allowing for the noise).
  Rerun with a longer `--warmup-s`.
- `ok`.

**Timer conditions.** The throughput and trace jobs run under `timeBeginPeriod(1)` (released on
exit, including on errors and Ctrl+C). Every output records `timer_resolution_ms` as Windows
reports it, whether 1 ms was requested, and a measured asyncio 1 ms sleep, so a run made under
different timer conditions is visible as such.

## Measurements

| # | Measurement | Result |
|---|---|---|
| 1 | Solo and combined throughput per link, with verdict | **NO-GO.** Mobile (iPhone, USB) 0.4-0.5 Mbps: effectively dead. Details below. |
| 2 | Recorded traces (two sessions, am and pm) | Pending: continuous, 20 min each, `--conns 2`. |
| 3 | Loopback ceiling (Python + httpx + event pipeline) | 3.0-3.1 Gbps with the pipeline, at 100% client CPU. Details below. |
| 4 | NTFS zero-fill cost at a high offset in a fresh large file | 1 MB at the end of a fresh 4 GiB file on D: took 22.5 s; 0.002 s when sparse. Details below. |
| 5 | `asyncio.sleep` accuracy vs the ~15.6 ms Windows timer | Default: every delay rounds up to the 15.6 ms tick (20 ms → 32 ms). Under `timeBeginPeriod(1)`: every delay from 1 ms met within ~2 ms at p95. Details below. |

### 1. Throughput (2026-11-01)

Wi-Fi from a home router; mobile is an iPhone over USB tethering. Three throughput runs, all
against public mirrors in Singapore:

| Run | Link | Solo before | Combined phase | Solo after |
|---|---|---|---|---|
| Hetzner, 4 s phases | mobile0 | 0.37 | 0.50 | 0.49 |
| | wifi0 | 17.6 | 30.8 | 3.5 |
| Hetzner, 8 s phases, Wi-Fi only | wifi0 | 20.1 | 8.9 | 15.4 |
| OVH, 8 s phases, Wi-Fi only | wifi0 | 31.6 | 15.0 | **0.0** |

Mbps. Ping to the router: 3-4 ms, 0% loss. Ping to 8.8.8.8: 34-59 ms (average 59 ms, one 336 ms
spike), 0% loss.

**The Wi-Fi numbers are not clean link measurements.** Every run requested 6 connections per link,
and both mirrors cap concurrent connections per client IP: the extra connections got HTTP 429 and
retried every second (the Wi-Fi link made 108 requests in one 3-phase run). OVH's 0.0 phase is a
server lockout, not a line dropout: after the stream of rejected requests it refused every
connection, and a later test reproduced exactly that. Hetzner's combined phase began with 2 s of
0.0, consistent with the previous phase's connections still counting against the cap. A later
run at 2 connections held 30.7 Mbps on Wi-Fi with no rejections, and a 1-minute continuous run
held 29.9-30.4 Mbps.

**The no-go does not depend on them.** The mobile link delivered 0.35-0.63 Mbps in every second
measured, with up to 2 connections downloading (its 25 requests were 6 successes and 19
rejections, one success per allowed slot per phase). That is not the server: from the same server,
each Wi-Fi connection carried about 15 Mbps. Whether the cause is signal or a carrier throttle on tethered
traffic was not determined, and does not matter for the verdict.

A later throughput run (2026-11-05) was made at a location with no mobile signal and on a
different Wi-Fi network, so it describes neither link above and is not part of this record: the
mobile link fell from 0.3 Mbps to nothing within the first phase.

### 3. Loopback ceiling (2026-11-01)

Server in a separate process, plain HTTP, 4 s per run after 1 s warm-up. "Raw" only reads the
body; "pipeline" adds 256 KB pieces, one JSON event per piece, 1 MB block assembly and a
byte-bounded queue, as the downloader will.

| Connections | Raw | Pipeline | Client CPU (pipeline) |
|---|---|---|---|
| 1 | 7304 Mbps | 3025 Mbps | 95% |
| 3 | 7417 Mbps | 3140 Mbps | 100% |
| 9 | 6766 Mbps | 3004 Mbps | 100% |

The client is CPU-bound in every case, so the pipeline, not the network stack, sets the ceiling.
The loopback tier keeps simulated aggregate rates below about 1.5 Gbps (half the ceiling) so
Python is never the binding constraint.

### 4. NTFS zero-fill (2026-11-01, D:, NTFS, fresh 4 GiB file, one 1 MB write)

| Case | Total | of which |
|---|---|---|
| write at offset 0 | 0.004 s | |
| pre-size with `os.ftruncate`, then write at the end | 4.365 s | 4.019 s pre-sizing |
| seek past the end, then write at the end | 22.489 s | 22.358 s in the write |
| `FSCTL_SET_SPARSE`, then write at the end | 0.002 s | |

`FSCTL_SET_SPARSE` is mandatory in the step 8 disk writer and pre-sizing with `truncate` is banned
([01-design.md, section 4](01-design.md#blocks-and-the-sink-coreblockspy-coresinkpy-coremem_sinkpy-coredisk_writerpy)).

### 5. Timer accuracy (2026-11-01)

Achieved `asyncio.sleep` time in ms, p50 / p95, 100 samples per delay. The monotonic clock's
resolution is 15.625 ms.

| Requested | Proactor (default) | Selector | Proactor + `timeBeginPeriod(1)` | Selector + `timeBeginPeriod(1)` |
|---|---|---|---|---|
| 1 ms | 15.9 / 16.3 | 16.0 / 16.3 | 1.1 / 2.0 | 1.0 / 2.0 |
| 5 ms | 16.0 / 16.3 | 16.0 / 16.3 | 6.2 / 6.8 | 5.2 / 5.9 |
| 15 ms | 19.5 / 32.2 | 16.0 / 16.3 | 16.2 / 16.8 | 15.2 / 15.9 |
| 20 ms | 32.0 / 32.4 | 32.0 / 32.4 | 21.2 / 22.1 | 20.2 / 20.9 |
| 50 ms | 63.9 / 64.6 | 63.9 / 64.6 | 50.2 / 50.9 | 50.2 / 50.8 |

Without `timeBeginPeriod(1)`, every delay rounds up to the next multiple of the 15.6 ms tick, so
the loopback shaper could not emulate RTT or jitter below that. With it, both loops meet every
tested delay within about 2 ms at p95; the selector loop is about 1 ms closer than the proactor.
Every network job, and the loopback tier, runs under `timeBeginPeriod(1)`.

## Verdict

**NO-GO on the real-world tier, recorded 2026-11-01.** There is one live link: the mobile link is
effectively dead at 0.4-0.5 Mbps, so nothing can be stacked with home Wi-Fi. As written above
before any result: this kills only the real-world sanity check. Scope is otherwise unchanged and
the simulator carries the result. The recorded traces are still taken: they are the simulator's
real-world input, and the mobile link's dropouts are exactly what they are for.
