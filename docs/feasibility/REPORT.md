# Feasibility results (redacted)

## interfaces

```
Interfaces with an IPv4 address (NIC speed is the negotiated rate: display only)
  name                         kind      up    ipv4              gateway         weakhostsend
  Local Area Connection* 1     virtual   False 169.254.229.117   -               False
  Local Area Connection* 11    virtual   False 169.254.15.8      -               False
  Wi-Fi                        wifi      True  192.168.1.36      192.168.1.1     False
  Bluetooth Network Connection virtual   False 169.254.198.164   -               False
  Ethernet                     ethernet  False 169.254.76.49     -               False
  Ethernet 2                   mobile    True  10.179.195.159    10.179.195.84   False

Suggested flags (check them; the kind is a guess from the adapter name):
  --link wifi0="Wi-Fi" --link mobile0="Ethernet 2" --mobile mobile0

Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  wi-fi      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi
  ethernet 2 Ethernet 2             ip=10.179.195.159  gw=10.179.195.84   kind=mobile MOBILE

```

## loopback

```
Loopback ceiling: server in a separate process (pid 31608), 4 s per run after 1 s warm-up, plain HTTP
  conns=1  raw         7303.7 Mbps   client CPU  94.2%   server CPU  33.6%
  conns=1  pipeline    3024.6 Mbps   client CPU  94.6%   server CPU  14.5%
  conns=3  raw         7416.9 Mbps   client CPU  99.9%   server CPU  32.8%
  conns=3  pipeline    3139.9 Mbps   client CPU  99.6%   server CPU  21.9%
  conns=9  raw         6765.9 Mbps   client CPU  99.8%   server CPU  27.4%
  conns=9  pipeline    3003.8 Mbps   client CPU  99.6%   server CPU  14.1%

Ceiling with the event pipeline: 3140 Mbps. Keep the loopback tier's simulated aggregate rates below about 1570 Mbps (half the ceiling) so Python is not the binding constraint.
```

## throughput-20261101-225513

```
Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  mobile0    Ethernet 3             ip=172.20.10.14    gw=172.20.10.1     kind=mobile MOBILE
  wifi0      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi

Throughput: 5 phases x 4 s, first 2 s of each discarded, 6 connections per link, http://sin-speed.hetzner.com/1GB.bin
  solo-before  mobile0    Mbps: mobile0=0.4
  solo-before  wifi0      Mbps: wifi0=17.6
  combined     all        Mbps: mobile0=0.5  wifi0=30.8
  solo-after   mobile0    Mbps: mobile0=0.5
  solo-after   wifi0      Mbps: wifi0=3.5

VERDICT: GO
  combined 31.26 Mbps vs best single link wifi0 10.58 Mbps = 2.955x (threshold 1.5x); stacking efficiency 284%
  mobile0: server IPs ['5.223.7.195'], left from ['172.20.10.14'] (expected 172.20.10.14)
  wifi0: server IPs ['5.223.7.195'], left from ['192.168.1.36'] (expected 192.168.1.36)
  note: mobile0: solo speed drifted from 0.4 to 0.5 Mbps between the before and after measurements, so every ratio here is approximate.
  note: wifi0: solo speed drifted from 17.6 to 3.5 Mbps between the before and after measurements, so every ratio here is approximate.
  note: Every link reached the same server IP and the links still stacked, so a shared server-side limit was not the binding constraint.
  A no-go kills only the real-world sanity check, not the project.
```

## throughput-20261101-225739

```
Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  wifi0      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi

Throughput: 3 phases x 8 s, first 3 s of each discarded, 6 connections per link, http://sin-speed.hetzner.com/1GB.bin
  solo-before  wifi0      Mbps: wifi0=20.1
  combined     all        Mbps: wifi0=8.9
  solo-after   wifi0      Mbps: wifi0=15.4

VERDICT: N/A
  wifi0: server IPs ['5.223.7.195'], left from ['192.168.1.36'] (expected 192.168.1.36)
  REASON: Need at least two links to test stacking.
  A no-go kills only the real-world sanity check, not the project.
```

## throughput-20261101-225925

```
Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  wifi0      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi

Throughput: 3 phases x 8 s, first 3 s of each discarded, 6 connections per link, https://sgp.proof.ovh.net/files/1Gb.dat
  solo-before  wifi0      Mbps: wifi0=31.6
  combined     all        Mbps: wifi0=15.0
  solo-after   wifi0      Mbps: wifi0=0.0

VERDICT: N/A
  wifi0: server IPs ['15.235.182.181'], left from ['192.168.1.36'] (expected 192.168.1.36)
  REASON: Need at least two links to test stacking.
  A no-go kills only the real-world sanity check, not the project.
```

## throughput-20261105-224254

```
Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  wifi0      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi
  mobile0    Ethernet 2             ip=10.179.195.159  gw=10.179.195.84   kind=mobile MOBILE

Throughput: 5 phases x 12 s, first 3 s of each discarded, 2 connections per link, http://sin-speed.hetzner.com/1GB.bin
  solo-before  wifi0      Mbps: wifi0=11.4 (2.0 of 2 conns)
  solo-before  mobile0    Mbps: mobile0=83.6 (1.9 of 2 conns)
WARNING: Mobile budget exhausted; remaining phases skipped.

VERDICT: N/A
  wifi0: server IPs ['5.223.7.195'], left from ['192.168.1.36'] (expected 192.168.1.36)
  mobile0: server IPs ['5.223.7.195'], left from ['10.179.195.159'] (expected 10.179.195.159)
  public IPs: 
    fewer than two public IPs known, so sharing cannot be checked
  REASON: Need at least two links to test stacking.
  A no-go kills only the real-world sanity check, not the project.
```

## throughput-20261105-224616

```
Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  wifi0      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi
  mobile0    Ethernet 2             ip=10.179.195.159  gw=10.179.195.84   kind=mobile MOBILE

Throughput: 5 phases x 8 s, first 2 s of each discarded, 2 connections per link, http://sin-speed.hetzner.com/1GB.bin
  solo-before  wifi0      Mbps: wifi0=30.6 (1.9 of 2 conns)
  solo-before  mobile0    Mbps: mobile0=2.7 (1.9 of 2 conns)
  combined     all        Mbps: wifi0=28.3 (1.9 of 2 conns)  mobile0=21.2 (1.9 of 2 conns)
  solo-after   wifi0      Mbps: wifi0=29.4 (1.9 of 2 conns)
  solo-after   mobile0    Mbps: mobile0=13.9 (1.9 of 2 conns)

VERDICT: INVALID
  combined 49.54 Mbps vs best single link wifi0 30.0 Mbps = 1.652x (threshold 1.5x); stacking efficiency 129%
  wifi0: server IPs ['5.223.7.195'], left from ['192.168.1.36'] (expected 192.168.1.36)
  mobile0: server IPs ['5.223.7.195'], left from ['10.179.195.159'] (expected 10.179.195.159)
  public IPs: wifi0=public-ip-C, mobile0=public-ip-D
    no two links share a public IP or a /24
  REASON: mobile0: solo speed went from 2.7 to 13.9 Mbps between the before and after measurements (5.10x, limit 1.5x), so the link changed under the test and no ratio computed from it holds.
  REASON: Combined (49.5 Mbps) exceeds the sum of the solo speeds (38.3 Mbps), stacking efficiency 129%. Links cannot add more together than apart, so conditions changed between phases.
  REASON: Rerun the throughput job; this run cannot support either verdict.
  A no-go kills only the real-world sanity check, not the project.
```

## throughput-20261105-224916

```
Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  wifi0      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi
  mobile0    Ethernet 2             ip=10.179.195.159  gw=10.179.195.84   kind=mobile MOBILE

Throughput: 5 phases x 8 s, first 2 s of each discarded, 2 connections per link, http://sin-speed.hetzner.com/1GB.bin
  solo-before  wifi0      Mbps: wifi0=30.4 (1.9 of 2 conns)
  solo-before  mobile0    Mbps: mobile0=1.1 (1.9 of 2 conns)
  combined     all        Mbps: wifi0=30.5 (1.9 of 2 conns)  mobile0=0.6 (1.2 of 2 conns)
  solo-after   wifi0      Mbps: wifi0=30.8 (1.9 of 2 conns)
  solo-after   mobile0    Mbps: mobile0=1.6 (1.9 of 2 conns)

VERDICT: NO-GO (SUSPECT: same server for all links)
  combined 31.11 Mbps vs best single link wifi0 30.56 Mbps = 1.018x (threshold 1.5x); stacking efficiency 98%
  wifi0: server IPs ['5.223.7.195'], left from ['192.168.1.36'] (expected 192.168.1.36)
  mobile0: server IPs ['5.223.7.195'], left from ['10.179.195.159'] (expected 10.179.195.159)
  public IPs: wifi0=public-ip-C, mobile0=public-ip-D
    no two links share a public IP or a /24
  REASON: Every link reached the same server IP (5.223.7.195). A limit on that server's side could be capping the combined total, so this no-go is not trustworthy yet: rerun with --url https://sgp.proof.ovh.net/files/1Gb.dat before accepting it.
  note: mobile0: solo speed drifted from 1.1 to 1.6 Mbps between the before and after measurements, so every ratio here is approximate.
  note: mobile0: fell to 47% of its solo speed while the other links were active (shared upstream, shared server limit, or a link that degrades under load).
  note: The links do stack (combined is 98% of the sum of solo speeds). The gain is small because the other links are slow compared with the best one, not because they interfere: reaching 1.5x needs the others to add up to at least 50% of the best link's speed.
  A no-go kills only the real-world sanity check, not the project.
```

## throughput-20261105-225311

```
Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  wifi0      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi
  mobile0    Ethernet 2             ip=10.179.195.159  gw=10.179.195.84   kind=mobile MOBILE

Throughput: 5 phases x 8 s, first 2 s of each discarded, 2 connections per link, https://sgp.proof.ovh.net/files/1Gb.dat
  solo-before  wifi0      Mbps: wifi0=28.1 (1.7 of 2 conns)
  solo-before  mobile0    Mbps: mobile0=29.0 (1.8 of 2 conns)
  combined     all        Mbps: wifi0=31.0 (1.8 of 2 conns)  mobile0=63.0 (1.8 of 2 conns)
  solo-after   wifi0      Mbps: wifi0=30.9 (1.8 of 2 conns)
  solo-after   mobile0    Mbps: mobile0=80.1 (1.8 of 2 conns)

VERDICT: INVALID
  combined 94.0 Mbps vs best single link mobile0 54.58 Mbps = 1.722x (threshold 1.5x); stacking efficiency 112%
  wifi0: server IPs ['15.235.182.181'], left from ['192.168.1.36'] (expected 192.168.1.36)
  mobile0: server IPs ['15.235.182.181'], left from ['10.179.195.159'] (expected 10.179.195.159)
  public IPs: wifi0=public-ip-C, mobile0=public-ip-D
    no two links share a public IP or a /24
  REASON: mobile0: solo speed went from 29.0 to 80.1 Mbps between the before and after measurements (2.76x, limit 1.5x), so the link changed under the test and no ratio computed from it holds.
  REASON: Combined (94.0 Mbps) exceeds the sum of the solo speeds (84.1 Mbps), stacking efficiency 112%. Links cannot add more together than apart, so conditions changed between phases.
  REASON: Rerun the throughput job; this run cannot support either verdict.
  A no-go kills only the real-world sanity check, not the project.
```

## timer

```
asyncio.sleep accuracy, 100 samples per delay; monotonic clock resolution 15.625 ms
  proactor (Windows default)
    requested    p5      p50     p95     p99     max   (achieved ms)
       1.0     1.90   15.86   16.26   22.58   28.56
       5.0    15.67   16.00   16.31   21.25   29.16
      15.0    16.02   19.45   32.15   32.23   32.25
      20.0    30.64   32.01   32.38   32.49   32.54
      50.0    60.97   63.87   64.56   64.83   67.95
  selector
    requested    p5      p50     p95     p99     max   (achieved ms)
       1.0     6.53   16.02   16.30   23.48   28.26
       5.0    15.23   15.97   16.28   16.30   16.50
      15.0    15.72   16.02   16.31   29.64   29.73
      20.0    30.94   32.02   32.43   33.13   45.45
      50.0    61.16   63.92   64.56   65.54   77.65
  proactor + timeBeginPeriod(1)
    requested    p5      p50     p95     p99     max   (achieved ms)
       1.0     0.97    1.10    2.04    2.12    2.53
       5.0     6.02    6.15    6.83    7.24    7.35
      15.0    16.04   16.21   16.77   16.88   17.02
      20.0    21.03   21.19   22.05   22.39   22.46
      50.0    50.06   50.20   50.90   51.01   51.15
  selector + timeBeginPeriod(1)
    requested    p5      p50     p95     p99     max   (achieved ms)
       1.0     0.97    1.03    2.03    2.51    2.55
       5.0     5.02    5.15    5.93    6.00    6.00
      15.0    15.05   15.22   15.94   16.01   16.02
      20.0    20.05   20.20   20.86   21.01   21.25
      50.0    50.03   50.22   50.83   51.10   51.13

  proactor (Windows default): smallest requested delay honoured within +2 ms at p95: none of those tested
  selector: smallest requested delay honoured within +2 ms at p95: 15 ms
  proactor + timeBeginPeriod(1): smallest requested delay honoured within +2 ms at p95: 1 ms
  selector + timeBeginPeriod(1): smallest requested delay honoured within +2 ms at p95: 1 ms
  Delays the loop cannot honour cannot be emulated as RTT or jitter by asyncio.sleep in the loopback shaper.
```

## trace-pm-20261105-231706

```
Pre-flight checks
  fMinimizeConnections = 0 (simultaneous connections allowed)
  wifi0      Wi-Fi                  ip=192.168.1.36    gw=192.168.1.1     kind=wifi
  mobile0    Ethernet 2             ip=10.179.195.159  gw=10.179.195.84   kind=mobile MOBILE

Trace session 'pm': continuous, 20 min: 10 s warm-up (saturated=0), then 1190 s saturated; 2 connections per link; progress every 30 s
  Projected mobile use: 8187 MB (budget 9000.0 MB)
  t=   30 s  mobile    2.0 MB  last 30 s Mbps: wifi0=21.4  mobile0=0.5
  t=   60 s  mobile    3.9 MB  last 30 s Mbps: wifi0=24.6  mobile0=0.5
  t=   90 s  mobile    7.5 MB  last 30 s Mbps: wifi0=17.8  mobile0=1.0
  t=  120 s  mobile   11.1 MB  last 30 s Mbps: wifi0=23.7  mobile0=1.0
  t=  150 s  mobile   13.3 MB  last 30 s Mbps: wifi0=27.9  mobile0=0.6
  t=  180 s  mobile   16.5 MB  last 30 s Mbps: wifi0=28.1  mobile0=0.9
  t=  210 s  mobile   18.5 MB  last 30 s Mbps: wifi0=21.0  mobile0=0.5
  t=  240 s  mobile   23.6 MB  last 30 s Mbps: wifi0=25.9  mobile0=1.4
  t=  270 s  mobile  298.0 MB  last 30 s Mbps: wifi0=24.6  mobile0=73.2
  t=  300 s  mobile  499.9 MB  last 30 s Mbps: wifi0=22.5  mobile0=53.9
  t=  330 s  mobile  672.8 MB  last 30 s Mbps: wifi0=30.1  mobile0=46.1
  t=  360 s  mobile  808.9 MB  last 30 s Mbps: wifi0=20.5  mobile0=36.3
  t=  390 s  mobile  913.3 MB  last 30 s Mbps: wifi0=26.9  mobile0=27.8
  t=  420 s  mobile 1095.4 MB  last 30 s Mbps: wifi0=28.7  mobile0=48.6
  t=  450 s  mobile 1286.3 MB  last 30 s Mbps: wifi0=24.1  mobile0=50.9
  t=  480 s  mobile 1431.8 MB  last 30 s Mbps: wifi0=30.3  mobile0=38.8
  t=  510 s  mobile 1698.8 MB  last 30 s Mbps: wifi0=25.7  mobile0=71.2
  t=  540 s  mobile 2052.9 MB  last 30 s Mbps: wifi0=25.5  mobile0=94.4
  t=  570 s  mobile 2573.3 MB  last 30 s Mbps: wifi0=29.6  mobile0=138.8
  t=  600 s  mobile 2805.9 MB  last 30 s Mbps: wifi0=26.1  mobile0=62.0
  t=  630 s  mobile 2981.6 MB  last 30 s Mbps: wifi0=27.5  mobile0=46.9
  t=  660 s  mobile 3121.1 MB  last 30 s Mbps: wifi0=28.8  mobile0=37.2
  t=  690 s  mobile 3125.6 MB  last 30 s Mbps: wifi0=25.9  mobile0=1.2
  t=  720 s  mobile 3128.1 MB  last 30 s Mbps: wifi0=29.8  mobile0=0.7
  t=  750 s  mobile 3286.9 MB  last 30 s Mbps: wifi0=23.3  mobile0=42.3
  t=  780 s  mobile 3521.3 MB  last 30 s Mbps: wifi0=25.4  mobile0=62.5
  t=  810 s  mobile 3534.5 MB  last 30 s Mbps: wifi0=30.2  mobile0=3.5
  t=  840 s  mobile 3543.9 MB  last 30 s Mbps: wifi0=28.8  mobile0=2.5
  t=  870 s  mobile 3546.5 MB  last 30 s Mbps: wifi0=27.6  mobile0=0.7
  t=  900 s  mobile 3549.7 MB  last 30 s Mbps: wifi0=27.7  mobile0=0.9
  t=  930 s  mobile 3552.5 MB  last 30 s Mbps: wifi0=27.5  mobile0=0.7
  t=  960 s  mobile 3557.2 MB  last 30 s Mbps: wifi0=30.2  mobile0=1.2
  t=  990 s  mobile 3561.4 MB  last 30 s Mbps: wifi0=26.9  mobile0=1.1
  t= 1020 s  mobile 3564.9 MB  last 30 s Mbps: wifi0=23.7  mobile0=0.9
  t= 1050 s  mobile 3567.1 MB  last 30 s Mbps: wifi0=30.0  mobile0=0.6
  t= 1080 s  mobile 3582.3 MB  last 30 s Mbps: wifi0=25.9  mobile0=4.0
  t= 1110 s  mobile 3583.2 MB  last 30 s Mbps: wifi0=28.7  mobile0=0.3
  t= 1140 s  mobile 3585.5 MB  last 30 s Mbps: wifi0=29.6  mobile0=0.6
  t= 1170 s  mobile 3587.6 MB  last 30 s Mbps: wifi0=26.9  mobile0=0.6
  t= 1200 s  mobile 3619.0 MB  last 30 s Mbps: wifi0=29.9  mobile0=8.4
Warm-up check (10 s warm-up; first saturated bin vs the rest of its window, limit 0.85):
  wifi0: NOT CHECKED, bin-to-bin noise too large over 1 window(s) to resolve a 15% shortfall (standard error 0.29)
  mobile0: NOT CHECKED, bin-to-bin noise too large over 1 window(s) to resolve a 15% shortfall (standard error 1.76)
  wifi0: 2.0 of 2 connections downloading on average; 4 requests on 2 local ports; 0 rejected by the server; errors: 0 link (outage), 0 measurement
  mobile0: 2.0 of 2 connections downloading on average; 5 requests on 3 local ports; 0 rejected by the server; errors: 1 link (outage), 0 measurement
Dropouts (saturated bins only; empty bytes = unmeasured, never zero):
  wifi0: 18 zero-byte bins of 11900, 0 outages >= 1 s (0 s total, longest 0.1 s), 0 bins unmeasured
  mobile0: 2594 zero-byte bins of 11900, 33 outages >= 1 s (49.5 s total, longest 4.6 s), 0 bins unmeasured
Wrote mobile-20261105-pm.csv and mobile-20261105-pm.meta.json in D:\Multi-Network Download Accelerator\feasibility-out\traces
```

## zerofill

```
Zero-fill: 4 GiB files in D:\ (filesystem NTFS)
  write-at-offset-0                    prepare 0.000  write 0.001  fsync 0.003  total 0.004 s
  presize-ftruncate-then-write-at-end  prepare 4.019  write 0.000  fsync 0.345  total 4.365 s
  seek-past-end-then-write             prepare 0.000  write 22.358  fsync 0.131  total 22.489 s
  sparse-then-write-at-end             prepare 0.000  write 0.001  fsync 0.001  total 0.002 s

Zero-fill MATTERS here: one 1 MB write at the end of a fresh 4 GiB file stalled for 22.49 s while NTFS filled the gap with zeros.
  With FSCTL_SET_SPARSE the same write took 0.002 s: the fix works.
  Pre-sizing with os.ftruncate cost 4.36 s: the writer must not pre-size the output file that way on Windows.
```
