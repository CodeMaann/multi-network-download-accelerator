# Feasibility (day 0)

**Status: script written, measurements not yet run.**

## What a no-go means

Written before any result exists: **a no-go kills only the real-world sanity check, not the
project.** All headline numbers come from the simulator, which does not depend on whether this
machine's links stack.

## Go/no-go criterion

Measure each interface's solo throughput (bound to its local IP), then all of them together, then
each solo again.

- **Go:** combined ≥ 1.5 × the best single link (the mean of its before and after solo runs).
- **No-go:** anything less.
- **Suspect:** a no-go where every link reached the same server IP. A limit on that server's side
  can't be ruled out, so the run is repeated against a second target before the no-go stands.
- **Invalid:** some link's connections did not leave from its own local IP.

The script also reports stacking efficiency (combined ÷ sum of solo speeds). High efficiency with
a no-go means the links stack but the others are slow compared with the best one. Low efficiency
means they interfere.

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

## How to run

From the repo root, in the project venv. Raw output goes to `feasibility-out/` (git-ignored).

```
# 0. See your adapters and the suggested --link flags; fix any warnings first.
.venv\Scripts\python scripts\feasibility.py interfaces

# 1. Throughput and verdict (about 7 phases x 12 s with three links; ~150-250 MB of mobile data).
.venv\Scripts\python scripts\feasibility.py throughput --link wifi0="Wi-Fi" --link eth0="Ethernet" --link mobile0="Ethernet 3" --mobile mobile0 --isp mobile0="<carrier>" --isp wifi0="<home ISP>" --budget-mb 300

#    If the verdict says SUSPECT, rerun against the alternate it names:
#    ... throughput <same flags> --url https://sgp.proof.ovh.net/files/1Gb.dat

# 2. Trace sessions, mid-morning and evening peak (10 min each, duty-cycled). The session prints
#    its projected mobile use and refuses to start if that exceeds the budget.
.venv\Scripts\python scripts\feasibility.py trace --session am <same --link/--mobile/--isp flags> --budget-mb 750 --conditions "weekday 10:00, indoors, 4 bars"
.venv\Scripts\python scripts\feasibility.py trace --session pm <same flags> --budget-mb 750 --conditions "weekday 20:30, indoors"

# 3-5. No network needed. Point zerofill at the drive you'll download to.
.venv\Scripts\python scripts\feasibility.py loopback
.venv\Scripts\python scripts\feasibility.py zerofill --dir D:\mnda-zerofill
.venv\Scripts\python scripts\feasibility.py timer

# Committable copies: docs/feasibility/ and scenarios/recorded/. Read them before `git add`.
.venv\Scripts\python scripts\feasibility.py redact
```

Mobile data used by every run accumulates in `feasibility-out/mobile-ledger.jsonl`.

## How to read the trace

**Connections are closed between duty cycles, not kept alive.** At the end of each cycle the
recorder cancels its downloads mid-body, which closes those connections (an HTTP/1.1 connection
with an unread body can't be reused). Every warm-up therefore starts from scratch: TCP handshake
plus slow start from the initial congestion window, with no cwnd carried over. The trace job
checks this instead of assuming it: it reports requests against distinct local ports per link
(`connection_reuse` in the meta file), and equal numbers mean every request used a new
connection. Even with keep-alive, slow-start restart would apply after 24 s idle, but a fresh
handshake costs an extra RTT or two, so the warm-up number should be read as "time from cold to
saturated, including connection setup".

**The warm-up check.** For each link, the trace job compares the first saturated 100 ms bin of
every window with the rest of that window (`warmup_check` in the output and the meta file). If the
first bin averages below 0.85 of the later bins, the connections were still ramping when the
saturated window began, the saturated bins understate capacity, and the job tells you to rerun
with a longer `--warmup-s`. `ratio_by_position` profiles the first five saturated bins, so a ramp
that runs past the first bin is visible too.

**Timer conditions.** The throughput and trace jobs run under `timeBeginPeriod(1)` (released on
exit, including on errors and Ctrl+C). Every output records `timer_resolution_ms` as Windows
reports it, whether 1 ms was requested, and a measured asyncio 1 ms sleep, so a run made under
different timer conditions is visible as such.

## Measurements

| # | Measurement | Result |
|---|---|---|
| 1 | Solo and combined throughput per link, with verdict | — |
| 2 | Recorded traces (two sessions, am and pm) | — |
| 3 | Loopback ceiling (Python + httpx + event pipeline) | — |
| 4 | NTFS zero-fill cost at a high offset in a fresh large file | — |
| 5 | `asyncio.sleep` accuracy vs the ~15.6 ms Windows timer | — |

Smoke runs on 2026-11-01 already showed that jobs 4 and 5 matter. They are summarised in
[01-design.md, section 7](01-design.md#what-the-feasibility-measurements-changed).

## Verdict

—
