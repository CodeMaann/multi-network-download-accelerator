"""Day-0 feasibility checks for the multi-network download accelerator.

Disposable script, not library code: nothing in src/mnda imports it. Results are written up in
docs/00-feasibility.md.

Jobs, in the order to run them:
  interfaces   list adapters, default routes and Windows binding pitfalls; suggests --link flags
  throughput   job 1: solo / all-links / solo throughput per link, with the go/no-go verdict
  trace        job 2: duty-cycled per-link throughput trace, replayed by the simulator
  loopback     job 3: ceiling of Python + httpx + an event pipeline over loopback
  zerofill     job 4: NTFS cost of a write near the end of a freshly created large file
  timer        job 5: asyncio.sleep accuracy under the real clock
  redact       write committable copies of the results into docs/feasibility/ and
               scenarios/recorded/ (public IPs removed, ISP names made generic)

Raw output goes to ./feasibility-out/, which is git-ignored because it contains your public IP
and ISP names. Only the output of `redact` (or of any job run with --redact) is meant to be
committed, and only after you have read it.

Mobile data: every job that touches the network takes --budget-mb, which counts bytes on links
marked --mobile only and stops the job hard when it is reached. MB here means 10^6 bytes, the
way carriers count. A running total across runs is kept in feasibility-out/mobile-ledger.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psutil

# --- constants -----------------------------------------------------------------------------

DEFAULT_URL = "http://sin-speed.hetzner.com/1GB.bin"
ALTERNATE_URLS = (
    "https://sgp.proof.ovh.net/files/1Gb.dat",
    "http://fsn1-speed.hetzner.com/1GB.bin",
    "https://proof.ovh.net/files/1Gb.dat",
)
PUBLIC_IP_URL = "http://api.ipify.org"
USER_AGENT = (
    "mnda-feasibility/0.1 (+https://github.com/CodeMaann/multi-network-download-accelerator)"
)

BIN_MS = 100
GO_RATIO = 1.5
DRIFT_LIMIT = 0.25  # solo before/after differing by more than this is reported as drift
CONTENTION_LIMIT = 0.7  # a link below this fraction of its solo speed while combined
STACKING_OK = 0.8  # combined / sum(solo) at or above this means the links do stack

MiB = 1024 * 1024
MB = 1_000_000
PIECE = 256 * 1024

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "feasibility-out"

JSONDict = dict[str, Any]

# --- small helpers -------------------------------------------------------------------------


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def mbps(nbytes: float, seconds: float) -> float:
    return nbytes * 8 / seconds / 1e6 if seconds > 0 else 0.0


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def git_sha() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return sha + ("-dirty" if dirty else "")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


class Report:
    """Console output that is also kept, so it can be saved, redacted and committed.

    Printed text is ASCII only: a piped Windows console uses cp1252 and would crash on more.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def say(self, line: str = "") -> None:
        print(line, flush=True)
        self.lines.append(line)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.say(f"WARNING: {message}")


def envelope(
    job: str,
    started: str,
    params: JSONDict,
    results: JSONDict,
    report: Report,
    *,
    timer: JSONDict,
) -> JSONDict:
    return {
        "job": job,
        "started_at": started,
        "finished_at": now_iso(),
        "recorder_git_sha": git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "timer": timer,
        "params": params,
        "results": results,
        "warnings": report.warnings,
        "report": report.lines,
    }


# --- timer environment ---------------------------------------------------------------------
# timeBeginPeriod(1) changes every latency-sensitive number (asyncio timers go from 15.6 ms
# ticks to ~1 ms), so it is an environment condition: every output records whether it was
# requested, what Windows reports, and what an asyncio 1 ms sleep actually took.

_active_timer_period_ms: int | None = None


@contextlib.contextmanager
def timer_period_1ms(enabled: bool = True) -> Iterator[None]:
    """timeBeginPeriod(1) for the duration of the block, always paired with timeEndPeriod(1),
    including when the block raises or is interrupted."""
    global _active_timer_period_ms
    if not enabled or sys.platform != "win32":
        yield
        return
    import ctypes

    winmm = ctypes.WinDLL("winmm")
    if winmm.timeBeginPeriod(1) != 0:  # TIMERR_NOERROR
        raise OSError("timeBeginPeriod(1) failed")
    _active_timer_period_ms = 1
    try:
        yield
    finally:
        _active_timer_period_ms = None
        winmm.timeEndPeriod(1)


def reported_timer_resolution_ms() -> float | None:
    """The current timer resolution Windows reports (NtQueryTimerResolution), in ms."""
    if sys.platform != "win32":
        return None
    import ctypes

    coarsest, finest, current = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
    try:
        status = ctypes.WinDLL("ntdll").NtQueryTimerResolution(
            ctypes.byref(coarsest), ctypes.byref(finest), ctypes.byref(current)
        )
    except (OSError, AttributeError):
        return None
    return current.value / 10_000 if status == 0 else None  # 100 ns units


async def _probe_sleep_1ms(samples: int) -> list[float]:
    got = []
    for _ in range(samples):
        t = time.perf_counter()
        await asyncio.sleep(0.001)
        got.append((time.perf_counter() - t) * 1000)
    return got


def timer_environment(samples: int = 20) -> JSONDict:
    """Measured timer conditions of this process right now. Call it inside the same
    timer_period_1ms() block as the measurement it describes."""
    probe = asyncio.run(_probe_sleep_1ms(samples))
    return {
        "timer_period_requested_ms": _active_timer_period_ms,
        "timer_resolution_ms": reported_timer_resolution_ms(),
        "asyncio_sleep_1ms_p50_ms": round(statistics.median(probe), 3),
    }


def append_ledger(out: Path, job: str, mobile_bytes: int, budget_mb: float | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ledger = out / "mobile-ledger.jsonl"
    entry = {"at": now_iso(), "job": job, "mobile_bytes": mobile_bytes, "budget_mb": budget_mb}
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    total = 0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError, KeyError):
            total += int(json.loads(line)["mobile_bytes"])
    print(
        f"Mobile data: {mobile_bytes / MB:.1f} MB this run, {total / MB:.1f} MB across all "
        f"feasibility runs (ledger: {ledger.name})",
        flush=True,
    )


# --- Windows network facts and pre-flight checks ------------------------------------------

_PS_FACTS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$o = [ordered]@{
  adapters = @(Get-NetAdapter | Select-Object Name, InterfaceDescription, Status)
  ipif     = @(Get-NetIPInterface -AddressFamily IPv4 | Select-Object InterfaceAlias,
               InterfaceMetric,
               @{n='WeakHostSend';e={[string]$_.WeakHostSend}},
               @{n='ConnectionState';e={[string]$_.ConnectionState}})
  routes   = @(Get-NetRoute -AddressFamily IPv4 |
               Where-Object {
                 @('0.0.0.0/0','0.0.0.0/1','128.0.0.0/1') -contains $_.DestinationPrefix } |
               Select-Object DestinationPrefix, InterfaceAlias, NextHop, RouteMetric)
}
$o | ConvertTo-Json -Depth 4 -Compress
"""


def windows_facts() -> JSONDict:
    if sys.platform != "win32":
        return {}
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_FACTS],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    try:
        facts = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return facts if isinstance(facts, dict) else {}


def min_connections_policy() -> int | None:
    """fMinimizeConnections from Group Policy; None if not set (Windows then uses its default)."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WcmSvc\GroupPolicy"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "fMinimizeConnections")
            return int(value)
    except OSError:
        return None


_MOBILE_HINTS = (
    "remote ndis",
    "rndis",
    "apple mobile device",
    "iphone",
    "android",
    "mobile broadband",
    "cellular",
    "wwan",
)
_VIRTUAL_HINTS = (
    "virtual",
    "vethernet",
    "hyper-v",
    "vmware",
    "virtualbox",
    "wsl",
    "bluetooth",
    "tap-",
    "wireguard",
    "tailscale",
    "zerotier",
    "wan miniport",
    "local area connection*",
)


def guess_kind(name: str, description: str | None) -> str:
    text = f"{name} {description or ''}".lower()
    if any(h in text for h in _MOBILE_HINTS):
        return "mobile"
    if any(h in text for h in _VIRTUAL_HINTS):
        return "virtual"
    if any(h in text for h in ("wi-fi", "wireless", "wlan", "802.11")):
        return "wifi"
    if "ethernet" in text:
        return "ethernet"
    return "unknown"


@dataclass
class IfaceInfo:
    name: str
    ipv4: list[str]
    is_up: bool
    nic_speed_mbps: int  # negotiated NIC rate: DISPLAY ONLY, never a throughput estimate
    description: str | None
    kind: str
    gateway: str | None
    route_metric: int | None
    interface_metric: int | None
    weak_host_send: bool | None

    @property
    def usable_ips(self) -> list[str]:
        return [ip for ip in self.ipv4 if not ip.startswith("169.254.")]

    def to_json(self) -> JSONDict:
        return {
            "name": self.name,
            "ipv4": self.ipv4,
            "is_up": self.is_up,
            "nic_speed_mbps_display_only": self.nic_speed_mbps,
            "description": self.description,
            "kind": self.kind,
            "gateway": self.gateway,
            "route_metric": self.route_metric,
            "interface_metric": self.interface_metric,
            "weak_host_send": self.weak_host_send,
        }


def gather_interfaces() -> tuple[list[IfaceInfo], JSONDict]:
    facts = windows_facts()
    adapters = {a.get("Name"): a for a in as_list(facts.get("adapters")) if isinstance(a, dict)}
    ipif = {i.get("InterfaceAlias"): i for i in as_list(facts.get("ipif")) if isinstance(i, dict)}
    defaults: dict[str, JSONDict] = {}
    for r in as_list(facts.get("routes")):
        if not isinstance(r, dict) or r.get("DestinationPrefix") != "0.0.0.0/0":
            continue
        alias = str(r.get("InterfaceAlias"))
        best = defaults.get(alias)
        if best is None or int(r.get("RouteMetric") or 0) < int(best.get("RouteMetric") or 0):
            defaults[alias] = r

    stats = psutil.net_if_stats()
    infos: list[IfaceInfo] = []
    for name, addrs in psutil.net_if_addrs().items():
        v4 = [a.address for a in addrs if a.family == socket.AF_INET]
        if not v4 or all(ip.startswith("127.") for ip in v4):
            continue
        st = stats.get(name)
        adapter = adapters.get(name, {})
        ip_iface = ipif.get(name, {})
        route = defaults.get(name)
        weak = ip_iface.get("WeakHostSend")
        description = adapter.get("InterfaceDescription")
        infos.append(
            IfaceInfo(
                name=name,
                ipv4=v4,
                is_up=bool(st.isup) if st else False,
                nic_speed_mbps=int(st.speed) if st else 0,
                description=description,
                kind=guess_kind(name, description),
                gateway=str(route.get("NextHop")) if route else None,
                route_metric=int(route.get("RouteMetric") or 0) if route else None,
                interface_metric=(
                    int(ip_iface["InterfaceMetric"]) if "InterfaceMetric" in ip_iface else None
                ),
                weak_host_send=None if weak is None else str(weak).lower() in ("enabled", "1"),
            )
        )
    return infos, facts


@dataclass(frozen=True)
class LinkSpec:
    id: str
    interface: str
    local_ip: str
    mobile: bool
    isp: str | None
    kind: str

    def to_json(self) -> JSONDict:
        return {
            "id": self.id,
            "interface": self.interface,
            "local_ip": self.local_ip,
            "mobile": self.mobile,
            "isp": self.isp,
            "kind": self.kind,
        }


_LINK_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


def parse_kv(values: Sequence[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key or not value:
            raise SystemExit(f"{flag} expects ID=VALUE, got {item!r}")
        out[key] = value
    return out


def parse_links(args: argparse.Namespace, infos: Sequence[IfaceInfo]) -> list[LinkSpec]:
    specs = parse_kv(args.link, "--link")
    if not specs:
        raise SystemExit(
            "No --link given. Run `python scripts/feasibility.py interfaces` to see your "
            'adapters and the suggested flags, e.g. --link wifi0=Wi-Fi --link mobile0="Ethernet 3"'
        )
    mobile_ids = set(args.mobile or [])
    isps = parse_kv(args.isp, "--isp")
    unknown = (mobile_ids | set(isps)) - set(specs)
    if unknown:
        raise SystemExit(
            f"--mobile/--isp name links that were not given with --link: {sorted(unknown)}"
        )

    by_name = {i.name: i for i in infos}
    links: list[LinkSpec] = []
    for link_id, iface in specs.items():
        if not _LINK_ID.match(link_id):
            raise SystemExit(f"link id {link_id!r} must be lowercase letters/digits, e.g. mobile0")
        info = by_name.get(iface)
        if info is None:
            raise SystemExit(f"no interface named {iface!r}. Known: {sorted(by_name)}")
        if not info.is_up:
            raise SystemExit(f"interface {iface!r} is down")
        if not info.usable_ips:
            raise SystemExit(
                f"interface {iface!r} only has a 169.254.x.x address: DHCP failed. For USB "
                "tethering, toggle tethering off and on and wait for a real address."
            )
        links.append(
            LinkSpec(
                id=link_id,
                interface=iface,
                local_ip=info.usable_ips[0],
                mobile=link_id in mobile_ids,
                isp=isps.get(link_id),
                kind=info.kind,
            )
        )
    return links


def require_budget(args: argparse.Namespace, links: Sequence[LinkSpec]) -> None:
    if any(link.mobile for link in links) and args.budget_mb is None:
        raise SystemExit("A link is marked --mobile, so --budget-mb is required.")
    if not any(link.mobile for link in links):
        print("Note: no link is marked --mobile, so --budget-mb limits nothing.", flush=True)


def preflight(
    links: Sequence[LinkSpec], infos: Sequence[IfaceInfo], facts: JSONDict, report: Report
) -> None:
    """Report the things that make a link read 0 MB/s or make two links secretly one."""
    report.say("Pre-flight checks")
    routes = [r for r in as_list(facts.get("routes")) if isinstance(r, dict)]
    split = [r for r in routes if r.get("DestinationPrefix") in ("0.0.0.0/1", "128.0.0.0/1")]
    if split:
        names = sorted({str(r.get("InterfaceAlias")) for r in split})
        report.warn(
            f"VPN-style split routes (0.0.0.0/1, 128.0.0.0/1) via {names}: disable the VPN."
        )

    if sys.platform == "win32":
        policy = min_connections_policy()
        if policy is None:
            report.say(
                "  fMinimizeConnections: not set by policy. Windows' default can soft-disconnect "
                "Wi-Fi while Ethernet is connected; if Wi-Fi drops when Ethernet is plugged in, "
                "set the policy to 0 (Allow simultaneous connections)."
            )
        elif policy != 0:
            report.warn(
                f"fMinimizeConnections = {policy}: Windows may disconnect Wi-Fi or mobile while "
                "Ethernet is up. Set it to 0 for these tests."
            )
        else:
            report.say("  fMinimizeConnections = 0 (simultaneous connections allowed)")

    if any(os.environ.get(v) for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")):
        report.say("  Proxy environment variables are set; this script ignores them on purpose.")

    by_name = {i.name: i for i in infos}
    for link in links:
        info = by_name[link.interface]
        if info.gateway is None:
            report.warn(
                f"{link.id} ({link.interface}) has no default route, so connections bound to "
                f"{link.local_ip} have nowhere to go. Check `route print` for a 0.0.0.0 line on it."
            )
        if info.weak_host_send:
            report.warn(
                f"{link.id} ({link.interface}) has WeakHostSend enabled: packets from its IP can "
                "leave through another interface and be dropped upstream."
            )
        report.say(
            f"  {link.id:<10} {link.interface:<22} ip={link.local_ip:<15} "
            f"gw={info.gateway or '-':<15} kind={info.kind}{' MOBILE' if link.mobile else ''}"
        )

    by_gateway: dict[str, list[str]] = {}
    for link in links:
        gw = by_name[link.interface].gateway
        if gw:
            by_gateway.setdefault(gw, []).append(link.id)
    for gw, ids in by_gateway.items():
        if len(ids) > 1:
            report.warn(
                f"{', '.join(ids)} all use gateway {gw}: they almost certainly share one upstream "
                "and will not add bandwidth to each other."
            )
    report.say()


# --- throughput measurement core ----------------------------------------------------------


class Bins:
    """Bytes per link per 100 ms bin, on one timeline that starts at t0 (loop.time())."""

    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self.data: dict[str, dict[int, int]] = {}
        self.max_index = -1

    def index(self, t: float) -> int:
        return max(0, int(round((t - self.t0) * 1000, 6)) // BIN_MS)

    def add(self, link_id: str, t: float, nbytes: int) -> None:
        idx = self.index(t)
        per_link = self.data.setdefault(link_id, {})
        per_link[idx] = per_link.get(idx, 0) + nbytes
        self.max_index = max(self.max_index, idx)

    def total(self, link_id: str, start_s: float = 0.0, end_s: float = math.inf) -> int:
        lo = start_s * 1000
        hi = end_s * 1000
        return sum(n for idx, n in self.data.get(link_id, {}).items() if lo <= idx * BIN_MS < hi)

    def series_mbps(
        self, link_id: str, start_s: float, end_s: float, step_s: float = 1.0
    ) -> list[float]:
        out = []
        t = start_s
        while t + step_s <= end_s + 1e-9:
            out.append(round(mbps(self.total(link_id, t, t + step_s), step_s), 3))
            t += step_s
        return out


class MobileBudget:
    def __init__(self, limit_mb: float | None, mobile_ids: Iterable[str]) -> None:
        self.limit = None if limit_mb is None else int(limit_mb * MB)
        self.mobile_ids = set(mobile_ids)
        self.used = 0
        self.exhausted = asyncio.Event()

    def charge(self, link_id: str, nbytes: int) -> None:
        if link_id not in self.mobile_ids:
            return
        self.used += nbytes
        if self.limit is not None and self.used >= self.limit:
            self.exhausted.set()


@dataclass
class LinkObs:
    """What the connections of one link actually did."""

    server_ips: set[str] = field(default_factory=set)
    client_ips: set[str] = field(default_factory=set)
    # Local TCP ports seen: as many ports as requests means every request opened a new
    # connection (nothing was kept alive across duty cycles).
    local_ports: set[int] = field(default_factory=set)
    statuses: set[int] = field(default_factory=set)
    final_urls: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    requests: int = 0

    def binding_ok(self, local_ip: str) -> bool:
        return self.client_ips == {local_ip}

    def to_json(self) -> JSONDict:
        return {
            "server_ips": sorted(self.server_ips),
            "client_ips": sorted(self.client_ips),
            "distinct_local_ports": len(self.local_ports),
            "http_statuses": sorted(self.statuses),
            "final_urls": sorted(self.final_urls),
            "requests": self.requests,
            "errors": self.errors,
        }


def make_client(link: LinkSpec, conns: int) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(
        local_address=link.local_ip,
        http1=True,
        http2=False,
        retries=0,
        limits=httpx.Limits(max_connections=conns, max_keepalive_connections=conns),
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
        trust_env=False,  # a proxy would silently defeat source-IP binding
        headers={"Accept-Encoding": "identity", "User-Agent": USER_AGENT},
    )


async def public_ip(client: httpx.AsyncClient) -> str | None:
    try:
        resp = await client.get(PUBLIC_IP_URL, timeout=8.0)
        ip = resp.text.strip()
        ipaddress.ip_address(ip)
    except (httpx.HTTPError, ValueError):
        return None
    return ip


async def _saturate_one(
    client: httpx.AsyncClient,
    url: str,
    link: LinkSpec,
    bins: Bins,
    budget: MobileBudget,
    obs: LinkObs,
) -> None:
    """One connection that downloads as fast as it can, forever, until cancelled."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            async with client.stream("GET", url) as resp:
                obs.requests += 1
                obs.statuses.add(resp.status_code)
                obs.final_urls.add(str(resp.url))
                stream = resp.extensions.get("network_stream")
                if stream is not None:
                    server = stream.get_extra_info("server_addr")
                    local = stream.get_extra_info("client_addr")
                    if server:
                        obs.server_ips.add(str(server[0]))
                    if local:
                        obs.client_ips.add(str(local[0]))
                        obs.local_ports.add(int(local[1]))
                if resp.status_code not in (200, 206):
                    if len(obs.errors) < 20:
                        obs.errors.append(f"HTTP {resp.status_code}")
                    await asyncio.sleep(1.0)
                    continue
                async for chunk in resp.aiter_raw():
                    bins.add(link.id, loop.time(), len(chunk))
                    budget.charge(link.id, len(chunk))
        except (httpx.HTTPError, OSError) as exc:
            if len(obs.errors) < 20:
                obs.errors.append(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(0.5)


async def run_load(
    clients: dict[str, httpx.AsyncClient],
    links: Sequence[LinkSpec],
    url: str,
    conns: int,
    seconds: float,
    bins: Bins,
    budget: MobileBudget,
    obs: dict[str, LinkObs],
) -> bool:
    """Saturate `links` with `conns` connections each for `seconds`. True if the budget ran out."""
    tasks = [
        asyncio.create_task(_saturate_one(clients[link.id], url, link, bins, budget, obs[link.id]))
        for link in links
        for _ in range(conns)
    ]
    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(budget.exhausted.wait(), seconds)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return budget.exhausted.is_set()


async def lookup_public_ips(
    clients: dict[str, httpx.AsyncClient], enabled: bool
) -> dict[str, str | None]:
    if not enabled:
        return {}
    ids = list(clients)
    ips = await asyncio.gather(*(public_ip(clients[i]) for i in ids))
    return dict(zip(ids, ips, strict=True))


# --- job 1: throughput + verdict ----------------------------------------------------------


def public_ip_sharing(public_ips: dict[str, str | None]) -> JSONDict:
    """Which links share a public IPv4 address (strong signal of one upstream), and which have
    different addresses in the same /24 (weaker signal: CGNAT pools). Pure, so it is tested."""
    exact: dict[str, list[str]] = {}
    by_24: dict[str, dict[str, set[str]]] = {}  # prefix -> link -> ip
    for link_id, ip in public_ips.items():
        if not ip:
            continue
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        exact.setdefault(ip, []).append(link_id)
        prefix = str(ipaddress.IPv4Network(f"{addr}/24", strict=False))
        by_24.setdefault(prefix, {}).setdefault(link_id, set()).add(ip)
    same_24 = []
    for prefix, links in by_24.items():
        distinct_ips = set().union(*links.values())
        if len(links) > 1 and len(distinct_ips) > 1:
            same_24.append({"links": sorted(links), "prefix": prefix})
    return {
        "exact": [sorted(m) for m in exact.values() if len(m) > 1],
        "same_24": same_24,
    }


def compute_verdict(
    solo_before: dict[str, float | None],
    solo_after: dict[str, float | None],
    combined: dict[str, float],
    server_ips: dict[str, set[str]],
    binding_ok: dict[str, bool],
    public_ips: dict[str, str | None],
    alternate_url: str = ALTERNATE_URLS[0],
) -> JSONDict:
    """The go/no-go verdict. Pure, so it is unit-tested."""
    ids = list(combined)
    verdict: JSONDict = {"status": "N/A", "go": None, "suspect": False, "reasons": [], "notes": []}
    reasons: list[str] = verdict["reasons"]
    notes: list[str] = verdict["notes"]
    if len(ids) < 2:
        reasons.append("Need at least two links to test stacking.")
        return verdict

    unbound = [i for i in ids if not binding_ok.get(i, False)]
    if unbound:
        verdict["status"] = "INVALID"
        reasons.extend(
            f"{i}: its connections did not all leave from its own local IP, so binding was "
            "defeated or nothing connected. Nothing measured on it means anything."
            for i in unbound
        )
        return verdict

    solo: dict[str, float] = {}
    for i in ids:
        vals = [v for v in (solo_before.get(i), solo_after.get(i)) if v is not None]
        solo[i] = sum(vals) / len(vals) if vals else 0.0
        if len(vals) == 2 and solo[i] > 0 and abs(vals[0] - vals[1]) / solo[i] > DRIFT_LIMIT:
            notes.append(
                f"{i}: solo speed drifted from {vals[0]:.1f} to {vals[1]:.1f} Mbps between the "
                "before and after measurements, so every ratio here is approximate."
            )
        if solo[i] == 0:
            notes.append(
                f"{i}: delivered nothing on its own. Try `curl.exe --interface <its local IP> "
                "-o NUL <url>`; if curl fails too, check `route print` for a 0.0.0.0 route on it."
            )

    best = max(ids, key=lambda i: solo[i])
    best_mbps = solo[best]
    if best_mbps <= 0:
        verdict["status"] = "INVALID"
        reasons.append("No link delivered any data on its own.")
        return verdict

    total = sum(combined.values())
    gain = total / best_mbps
    efficiency = total / sum(solo.values())
    go = gain >= GO_RATIO
    verdict.update(
        go=go,
        best_link=best,
        best_single_mbps=round(best_mbps, 2),
        combined_mbps=round(total, 2),
        gain=round(gain, 3),
        stacking_efficiency=round(efficiency, 3),
        solo_mbps={i: round(v, 2) for i, v in solo.items()},
    )

    for i in ids:
        if solo[i] > 0 and combined[i] / solo[i] < CONTENTION_LIMIT:
            notes.append(
                f"{i}: fell to {combined[i] / solo[i]:.0%} of its solo speed while the other "
                "links were active (shared upstream, shared server limit, or a link that "
                "degrades under load)."
            )

    sharing = public_ip_sharing(public_ips)
    verdict["public_ip_sharing"] = sharing
    for members in sharing["exact"]:
        notes.append(
            f"{', '.join(members)} share one public IP, so they share one upstream and can "
            "add no more than that upstream's capacity together."
        )
    for group in sharing["same_24"]:
        notes.append(
            f"WEAKER SIGNAL: {', '.join(group['links'])} have different public IPs in the same "
            f"/24 ({group['prefix']}). Carrier-grade NAT can hand out different addresses from "
            "one upstream block, so they may share a path even though their IPs differ."
        )

    distinct = set().union(*server_ips.values()) if server_ips else set()
    same_server = len(distinct) == 1 and all(server_ips.get(i) == distinct for i in ids)
    verdict["same_server_ip"] = same_server
    verdict["server_ips"] = {i: sorted(server_ips.get(i, set())) for i in ids}
    if same_server and not go:
        verdict["suspect"] = True
        reasons.append(
            f"Every link reached the same server IP ({next(iter(distinct))}). A limit on that "
            "server's side could be capping the combined total, so this no-go is not "
            f"trustworthy yet: rerun with --url {alternate_url} before accepting it."
        )
    elif same_server:
        notes.append(
            "Every link reached the same server IP and the links still stacked, so a shared "
            "server-side limit was not the binding constraint."
        )
    else:
        notes.append("The links reached different server IPs.")

    if not go:
        if efficiency >= STACKING_OK:
            notes.append(
                f"The links do stack (combined is {efficiency:.0%} of the sum of solo speeds). "
                "The gain is small because the other links are slow compared with the best "
                f"one, not because they interfere: reaching {GO_RATIO}x needs the others to add "
                f"up to at least {GO_RATIO - 1:.0%} of the best link's speed."
            )
        else:
            notes.append(
                f"The links interfere: combined is only {efficiency:.0%} of the sum of solo "
                "speeds (shared upstream, shared server limit, or a link that drops under load)."
            )

    if go:
        verdict["status"] = "GO"
    elif verdict["suspect"]:
        verdict["status"] = "NO-GO (SUSPECT: same server for all links)"
    else:
        verdict["status"] = "NO-GO"
    return verdict


async def throughput_async(
    args: argparse.Namespace, links: Sequence[LinkSpec], report: Report
) -> JSONDict:
    loop = asyncio.get_running_loop()
    budget = MobileBudget(args.budget_mb, [link.id for link in links if link.mobile])
    clients = {link.id: make_client(link, args.conns) for link in links}
    obs = {link.id: LinkObs() for link in links}
    by_id = {link.id: link for link in links}
    phases: list[tuple[str, str, list[LinkSpec]]] = (
        [("solo-before", link.id, [link]) for link in links]
        + [("combined", "all", list(links))]
        + [("solo-after", link.id, [link]) for link in links]
    )
    phase_results: list[JSONDict] = []
    try:
        public_ips = await lookup_public_ips(clients, not args.no_public_ip)
        report.say(
            f"Throughput: {len(phases)} phases x {args.duration:g} s, first {args.warmup:g} s "
            f"of each discarded, {args.conns} connections per link, {args.url}"
        )
        for kind, who, active in phases:
            if budget.exhausted.is_set():
                report.warn("Mobile budget exhausted; remaining phases skipped.")
                break
            bins = Bins(loop.time())
            stopped = await run_load(
                clients, active, args.url, args.conns, args.duration, bins, budget, obs
            )
            elapsed = loop.time() - bins.t0
            window = min(args.duration, elapsed) - args.warmup
            rates = {
                link.id: (mbps(bins.total(link.id, args.warmup), window) if window > 0 else None)
                for link in active
            }
            shown = "  ".join(
                f"{i}={r:.1f}" if r is not None else f"{i}=n/a" for i, r in rates.items()
            )
            report.say(f"  {kind:<12} {who:<10} Mbps: {shown}")
            phase_results.append(
                {
                    "phase": kind,
                    "link": who,
                    "measured_seconds": round(max(window, 0.0), 3),
                    "mbps": rates,
                    "series_1s_mbps": {
                        link.id: bins.series_mbps(link.id, args.warmup, min(args.duration, elapsed))
                        for link in active
                    },
                    "budget_stopped": stopped,
                }
            )
    finally:
        await asyncio.gather(*(c.aclose() for c in clients.values()))

    def solo_of(kind: str) -> dict[str, float | None]:
        return {p["link"]: p["mbps"][p["link"]] for p in phase_results if p["phase"] == kind}

    combined_phase = next((p for p in phase_results if p["phase"] == "combined"), None)
    combined = (
        {i: float(v or 0.0) for i, v in combined_phase["mbps"].items()} if combined_phase else {}
    )
    verdict = compute_verdict(
        solo_before=solo_of("solo-before"),
        solo_after=solo_of("solo-after"),
        combined=combined,
        server_ips={i: o.server_ips for i, o in obs.items()},
        binding_ok={i: o.binding_ok(by_id[i].local_ip) for i, o in obs.items()},
        public_ips=public_ips,
    )
    report.say()
    report.say(f"VERDICT: {verdict['status']}")
    if verdict.get("gain") is not None:
        report.say(
            f"  combined {verdict['combined_mbps']} Mbps vs best single link "
            f"{verdict['best_link']} {verdict['best_single_mbps']} Mbps = {verdict['gain']}x "
            f"(threshold {GO_RATIO}x); stacking efficiency {verdict['stacking_efficiency']:.0%}"
        )
    for i, o in obs.items():
        report.say(
            f"  {i}: server IPs {sorted(o.server_ips) or '-'}, "
            f"left from {sorted(o.client_ips) or '-'} (expected {by_id[i].local_ip})"
        )
    for line in verdict["reasons"]:
        report.say(f"  REASON: {line}")
    for line in verdict["notes"]:
        report.say(f"  note: {line}")
    report.say("  A no-go kills only the real-world sanity check, not the project.")
    return {
        "verdict": verdict,
        "phases": phase_results,
        "links": [link.to_json() for link in links],
        "observed": {i: o.to_json() for i, o in obs.items()},
        "public_ips": public_ips,
        "mobile_bytes": budget.used,
    }


# --- job 2: duty-cycled trace recorder ----------------------------------------------------


def ceil_bin_ms(seconds: float) -> int:
    return math.ceil(round(seconds * 1000, 6) / BIN_MS) * BIN_MS


def floor_bin_ms(seconds: float) -> int:
    return math.floor(round(seconds * 1000, 6) / BIN_MS) * BIN_MS


def saturated_flags(n_bins: int, windows_ms: Sequence[tuple[int, int]]) -> list[int]:
    """1 for bins lying entirely inside a saturated window [start_ms, end_ms), else 0."""
    flags = [0] * n_bins
    for start, end in windows_ms:
        for k in range(start // BIN_MS, min(end // BIN_MS, n_bins)):
            if k * BIN_MS >= start and (k + 1) * BIN_MS <= end:
                flags[k] = 1
    return flags


def trace_rows(
    link_ids: Sequence[str], data: dict[str, dict[int, int]], n_bins: int, flags: Sequence[int]
) -> list[tuple[int, str, int, int]]:
    """Dense rows: every link has a row for every bin, zeros included."""
    return [
        (k * BIN_MS, link_id, data.get(link_id, {}).get(k, 0), flags[k])
        for k in range(n_bins)
        for link_id in link_ids
    ]


def is_bin_multiple(seconds: float) -> bool:
    return abs(seconds * 1000 / BIN_MS - round(seconds * 1000 / BIN_MS)) < 1e-9


WARMUP_RATIO_LIMIT = 0.85  # first saturated bin below this fraction of the rest = still ramping
WARMUP_POSITIONS = 5  # how many leading saturated bins to profile


def warmup_check(
    data: dict[str, dict[int, int]],
    windows_ms: Sequence[tuple[int, int]],
    link_ids: Sequence[str],
) -> JSONDict:
    """Was the warm-up long enough? Per link, compare the first saturated bin of every window
    with the window's later bins. If the first bin is materially lower, connections were still
    ramping up when the saturated window began, and the saturated bins understate capacity.
    Pure, so it is unit-tested."""
    per_link: dict[str, JSONDict] = {}
    for link_id in link_ids:
        bins = data.get(link_id, {})
        firsts: list[int] = []
        laters: list[int] = []
        by_position: list[list[int]] = [[] for _ in range(WARMUP_POSITIONS)]
        for start, end in windows_ms:
            values = [bins.get(k, 0) for k in range(start // BIN_MS, end // BIN_MS)]
            if len(values) < 2:
                continue
            firsts.append(values[0])
            laters.extend(values[1:])
            for pos, v in enumerate(values[:WARMUP_POSITIONS]):
                by_position[pos].append(v)
        later_mean = statistics.fmean(laters) if laters else 0.0
        if not firsts or later_mean <= 0:
            per_link[link_id] = {"status": "n/a", "windows": len(firsts)}
            continue
        first_mean = statistics.fmean(firsts)
        ratio = first_mean / later_mean
        per_link[link_id] = {
            "status": "insufficient" if ratio < WARMUP_RATIO_LIMIT else "ok",
            "windows": len(firsts),
            "first_bin_mean_mbps": round(mbps(first_mean, BIN_MS / 1000), 3),
            "later_bins_mean_mbps": round(mbps(later_mean, BIN_MS / 1000), 3),
            "first_to_later_ratio": round(ratio, 3),
            "ratio_by_position": [
                round(statistics.fmean(p) / later_mean, 3) for p in by_position if p
            ],
        }
    statuses = [v["status"] for v in per_link.values()]
    overall = (
        "insufficient"
        if "insufficient" in statuses
        else ("ok" if statuses and all(s == "ok" for s in statuses) else "n/a")
    )
    return {"status": overall, "ratio_limit": WARMUP_RATIO_LIMIT, "links": per_link}


def report_warmup(check: JSONDict, warmup_s: float, report: Report) -> None:
    report.say(
        f"Warm-up check ({warmup_s:g} s warm-up; first saturated bin vs the rest of its window, "
        f"limit {check['ratio_limit']}):"
    )
    for link_id, r in check["links"].items():
        if r["status"] == "n/a":
            report.say(f"  {link_id}: n/a (no usable saturated windows)")
            continue
        report.say(
            f"  {link_id}: first bin {r['first_bin_mean_mbps']:.1f} Mbps vs later "
            f"{r['later_bins_mean_mbps']:.1f} Mbps = {r['first_to_later_ratio']:.2f} "
            f"over {r['windows']} windows -> {r['status'].upper()}"
        )
    if check["status"] == "insufficient":
        report.warn(
            "Warm-up was insufficient: the first saturated bins are still ramping up, so the "
            f"saturated bins understate capacity. Rerun with a longer --warmup-s (e.g. "
            f"{warmup_s + 1:g})."
        )


@dataclass
class TraceState:
    links: list[LinkSpec]
    budget: MobileBudget
    bins: Bins | None = None
    windows_ms: list[tuple[int, int]] = field(default_factory=list)
    end_s: float = 0.0
    truncated: str | None = None
    obs: dict[str, LinkObs] = field(default_factory=dict)
    public_ips: dict[str, str | None] = field(default_factory=dict)
    started_at: str = ""
    timer: JSONDict = field(default_factory=dict)
    warmup: JSONDict = field(default_factory=dict)
    connection_reuse: JSONDict = field(default_factory=dict)


async def trace_async(args: argparse.Namespace, state: TraceState, report: Report) -> None:
    loop = asyncio.get_running_loop()
    links = state.links
    period = args.warmup_s + args.burst_s + args.idle_s
    cycles = int(args.minutes * 60 // period)
    clients = {link.id: make_client(link, args.conns) for link in links}
    state.obs = {link.id: LinkObs() for link in links}
    try:
        state.public_ips = await lookup_public_ips(clients, not args.no_public_ip)
        state.started_at = now_iso()
        t0 = loop.time()
        state.bins = Bins(t0)
        for cycle in range(cycles):
            await asyncio.sleep(max(0.0, t0 + cycle * period - loop.time()))
            if state.budget.exhausted.is_set():
                state.truncated = "budget"
                break
            start = loop.time() - t0
            stopped = await run_load(
                clients,
                links,
                args.url,
                args.conns,
                args.warmup_s + args.burst_s,
                state.bins,
                state.budget,
                state.obs,
            )
            end = loop.time() - t0
            ws = ceil_bin_ms(start + args.warmup_s)
            we = min(ws + round(args.burst_s * 1000), floor_bin_ms(end))
            if we > ws:
                state.windows_ms.append((ws, we))
            state.end_s = end
            window_s = (we - ws) / 1000
            rates = "  ".join(
                f"{link.id}={mbps(state.bins.total(link.id, ws / 1000, we / 1000), window_s):.1f}"
                for link in links
            )
            report.say(
                f"  cycle {cycle + 1:>2}/{cycles}  mobile {state.budget.used / MB:7.1f} MB  "
                f"saturated Mbps: {rates}"
            )
            if stopped:
                state.truncated = "budget"
                report.warn("Mobile budget reached; stopping the session early.")
                break
        else:
            await asyncio.sleep(max(0.0, t0 + cycles * period - loop.time()))
            state.end_s = cycles * period
    finally:
        await asyncio.gather(*(c.aclose() for c in clients.values()))


def project_trace_mobile_bytes(
    out: Path, links: Sequence[LinkSpec], seconds_loaded: float
) -> float | None:
    """Projected mobile bytes for a session, from the latest throughput run's solo speeds."""
    latest = out / "throughput-latest.json"
    if not latest.exists():
        return None
    try:
        solo = json.loads(latest.read_text(encoding="utf-8"))["results"]["verdict"]["solo_mbps"]
    except (KeyError, ValueError, TypeError):
        return None
    total = 0.0
    for link in links:
        if link.mobile:
            if link.id not in solo:
                return None
            total += float(solo[link.id]) * 1e6 / 8 * seconds_loaded
    return total


def write_trace(out: Path, args: argparse.Namespace, state: TraceState) -> tuple[Path, Path]:
    links = state.links
    planned = int(args.minutes * 60 // (args.warmup_s + args.burst_s + args.idle_s)) * (
        args.warmup_s + args.burst_s + args.idle_s
    )
    bins = state.bins
    end_s = state.end_s
    if state.truncated == "interrupted" and bins is not None:
        end_s = (bins.max_index + 1) * BIN_MS / 1000
    n_bins = ceil_bin_ms(end_s) // BIN_MS
    flags = saturated_flags(n_bins, state.windows_ms)
    data = bins.data if bins is not None else {}
    rows = trace_rows([link.id for link in links], data, n_bins, flags)

    name = f"mobile-{dt.date.today():%Y%m%d}-{args.session}"
    csv_path = out / "traces" / f"{name}.csv"
    meta_path = out / "traces" / f"{name}.meta.json"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        f.write("t_ms,link,bytes,saturated\n")
        f.writelines(f"{t},{link_id},{n},{s}\n" for t, link_id, n, s in rows)

    meta = {
        "recorded_at": state.started_at,
        "duration_s": round(n_bins * BIN_MS / 1000, 3),
        "planned_duration_s": planned,
        "truncated": state.truncated,
        "sample_ms": BIN_MS,
        "connections_per_link": args.conns,
        "duty_cycle": {
            "warmup_s": args.warmup_s,
            "saturated_s": args.burst_s,
            "idle_s": args.idle_s,
            "period_s": args.warmup_s + args.burst_s + args.idle_s,
            "saturated_windows_ms": state.windows_ms,
        },
        "semantics": (
            "Goodput, replayed as capacity. bytes = bytes delivered to the application in the "
            "100 ms bin. saturated = 1 only for bins entirely inside a saturated window; warm-up "
            "bins carry bytes but saturated = 0 (connection setup and slow start), idle bins are "
            "0/0 because nothing was being measured, not because the link stalled. A single "
            "sample of the author's own real conditions."
        ),
        "links": [
            {
                **link.to_json(),
                "server_ips": sorted(state.obs.get(link.id, LinkObs()).server_ips),
                "binding_verified": state.obs.get(link.id, LinkObs()).binding_ok(link.local_ip),
            }
            for link in links
        ],
        "public_ips": state.public_ips,
        "server_url": args.url,
        "final_urls": sorted(set().union(*(o.final_urls for o in state.obs.values()))),
        "total_bytes_per_link": {link.id: sum(data.get(link.id, {}).values()) for link in links},
        "mobile_bytes": state.budget.used,
        "budget_mb": args.budget_mb,
        "recorder_git_sha": git_sha(),
        "timer_resolution_ms": state.timer.get("timer_resolution_ms"),
        "timer": state.timer,
        "connections_between_cycles": (
            "closed: each cycle cancels its downloads mid-body, which closes the connections, so "
            "every warm-up starts with new TCP connections (handshake + slow start from the "
            "initial window). connection_reuse shows requests vs distinct local ports."
        ),
        "connection_reuse": state.connection_reuse,
        "warmup_check": state.warmup,
        "conditions": args.conditions,
    }
    write_json(meta_path, meta)
    return csv_path, meta_path


# --- job 3: loopback ceiling ----------------------------------------------------------------


async def _serve(port: int) -> None:
    block = memoryview(bytes(MiB))
    size = 1 << 40  # 1 TiB: the client always hangs up first

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                b"Content-Length: %d\r\n\r\n" % size
            )
            while True:
                writer.write(block)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    print(server.sockets[0].getsockname()[1], flush=True)
    async with server:
        await server.serve_forever()


async def _loopback_conn(
    client: httpx.AsyncClient,
    url: str,
    pipeline: bool,
    counter: list[int],
    queue: asyncio.Queue[bytes],
    events: list[str],
    conn_id: int,
) -> None:
    loop = asyncio.get_running_loop()
    pending = bytearray()
    block = bytearray()
    while True:
        async with client.stream("GET", url) as resp:
            async for chunk in resp.aiter_raw():
                counter[0] += len(chunk)
                if not pipeline:
                    continue
                # What the real engine will do per connection: cut 256 KB pieces, log an event
                # per piece, assemble whole 1 MB blocks, hand them to a byte-bounded queue.
                pending += chunk
                while len(pending) >= PIECE:
                    piece = bytes(pending[:PIECE])
                    del pending[:PIECE]
                    events.append(
                        json.dumps(
                            {"t": loop.time(), "event": "progress", "conn": conn_id, "bytes": PIECE}
                        )
                    )
                    if len(events) > 50_000:
                        events.clear()
                    block += piece
                    if len(block) >= MiB:
                        await queue.put(bytes(block))
                        block.clear()


def _tree(proc: psutil.Process) -> list[psutil.Process]:
    # In a venv on Windows, .venv\Scripts\python.exe is a launcher that runs the real
    # interpreter as a child process, so the server's CPU is in its children.
    with contextlib.suppress(psutil.Error):
        return [proc, *proc.children(recursive=True)]
    return [proc]


def _cpu_s(procs: Iterable[psutil.Process]) -> float:
    total = 0.0
    for p in procs:
        with contextlib.suppress(psutil.Error):
            t = p.cpu_times()
            total += t.user + t.system
    return total


async def loopback_async(
    args: argparse.Namespace, port: int, server_pid: int, report: Report
) -> JSONDict:
    url = f"http://127.0.0.1:{port}/blob"
    me = psutil.Process()
    server = psutil.Process(server_pid)
    rows: list[JSONDict] = []
    for conns in args.conns_list:
        for pipeline in (False, True):
            client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(
                    http1=True,
                    http2=False,
                    limits=httpx.Limits(max_connections=conns, max_keepalive_connections=conns),
                ),
                timeout=10.0,
                trust_env=False,
            )
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)  # 64 x 1 MB blocks
            counter = [0]
            events: list[str] = []
            sunk = [0]

            async def sink(q: asyncio.Queue[bytes] = queue, total: list[int] = sunk) -> None:
                while True:
                    total[0] += len(await q.get())

            tasks = [asyncio.create_task(sink())] + [
                asyncio.create_task(
                    _loopback_conn(client, url, pipeline, counter, queue, events, c)
                )
                for c in range(conns)
            ]
            await asyncio.sleep(args.warmup)
            b0, c0, s0, t0 = counter[0], _cpu_s([me]), _cpu_s(_tree(server)), time.perf_counter()
            await asyncio.sleep(args.seconds)
            b1, c1, s1, t1 = counter[0], _cpu_s([me]), _cpu_s(_tree(server)), time.perf_counter()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            wall = t1 - t0
            row = {
                "connections": conns,
                "mode": "pipeline" if pipeline else "raw",
                "mbps": round(mbps(b1 - b0, wall), 1),
                "client_cpu_pct": round((c1 - c0) / wall * 100, 1),
                "server_cpu_pct": round((s1 - s0) / wall * 100, 1),
            }
            rows.append(row)
            report.say(
                f"  conns={conns:<2} {row['mode']:<8} {row['mbps']:>9.1f} Mbps   "
                f"client CPU {row['client_cpu_pct']:>5.1f}%   "
                f"server CPU {row['server_cpu_pct']:>5.1f}%"
            )
    pipeline_rows = [r for r in rows if r["mode"] == "pipeline"]
    ceiling = max(r["mbps"] for r in pipeline_rows)
    report.say()
    report.say(
        f"Ceiling with the event pipeline: {ceiling:.0f} Mbps. Keep the loopback tier's simulated "
        f"aggregate rates below about {ceiling / 2:.0f} Mbps (half the ceiling) so Python is not "
        "the binding constraint."
    )
    if any(r["server_cpu_pct"] >= 90 for r in rows):
        report.warn(
            "The loopback server reached ~100% CPU in some runs, so those numbers are a lower "
            "bound on the client's ceiling."
        )
    return {"rows": rows, "pipeline_ceiling_mbps": ceiling}


# --- job 4: NTFS zero-fill -------------------------------------------------------------------


def _fs_type(path: Path) -> str:
    target = str(path.resolve()).lower()
    best = ("", "unknown")
    for part in psutil.disk_partitions(all=False):
        mount = part.mountpoint.lower()
        if target.startswith(mount) and len(mount) > len(best[0]):
            best = (mount, part.fstype or "unknown")
    return best[1]


ZEROFILL_CASES = {
    "write-at-offset-0": "baseline: 1 MB at the start of a fresh file",
    "presize-ftruncate-then-write-at-end": (
        "pre-sizing with os.ftruncate first (on Windows the C runtime writes the zeros itself)"
    ),
    "seek-past-end-then-write": "1 MB at the end of a fresh file: NTFS zero-fills the gap",
    "sparse-then-write-at-end": "the same, after FSCTL_SET_SPARSE (the candidate fix)",
}


def _set_sparse(fd: int) -> None:
    if sys.platform != "win32":
        raise OSError("FSCTL_SET_SPARSE is Windows-only")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    fsctl_set_sparse = 0x000900C4
    returned = wintypes.DWORD()
    ok = ctypes.windll.kernel32.DeviceIoControl(
        wintypes.HANDLE(msvcrt.get_osfhandle(fd)),
        fsctl_set_sparse,
        None,
        0,
        None,
        0,
        ctypes.byref(returned),
        None,
    )
    if not ok:
        raise ctypes.WinError()


def _timed_write_case(path: Path, size: int, mode: str) -> JSONDict:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    payload = os.urandom(MiB)
    fd = os.open(path, flags)
    try:
        t = time.perf_counter()
        if mode == "presize-ftruncate-then-write-at-end":
            os.ftruncate(fd, size)
        elif mode == "sparse-then-write-at-end":
            _set_sparse(fd)
        t_size = time.perf_counter() - t
        offset = 0 if mode == "write-at-offset-0" else size - MiB
        t = time.perf_counter()
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, payload)
        t_write = time.perf_counter() - t
        t = time.perf_counter()
        os.fsync(fd)
        t_fsync = time.perf_counter() - t
    finally:
        t = time.perf_counter()
        os.close(fd)
        t_close = time.perf_counter() - t
        with contextlib.suppress(OSError):
            path.unlink()
    return {
        "case": mode,
        "what": ZEROFILL_CASES[mode],
        "offset": offset,
        "prepare_s": round(t_size, 4),
        "write_s": round(t_write, 4),
        "fsync_s": round(t_fsync, 4),
        "close_s": round(t_close, 4),
        "total_s": round(t_size + t_write + t_fsync + t_close, 4),
    }


def zerofill_job(args: argparse.Namespace, report: Report) -> JSONDict:
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    size = int(args.size_gb * 1024**3)
    free = shutil.disk_usage(directory).free
    if free < size + 1024**3:
        raise SystemExit(
            f"Need {(size + 1024**3) / 1024**3:.1f} GiB free in {directory}, "
            f"have {free / 1024**3:.1f}."
        )
    fs = _fs_type(directory)
    report.say(f"Zero-fill: {args.size_gb:g} GiB files in {directory} (filesystem {fs})")
    modes = [m for m in ZEROFILL_CASES if sys.platform == "win32" or not m.startswith("sparse")]
    cases: dict[str, JSONDict] = {}
    for mode in modes:
        path = directory / f"mnda-zerofill-{mode}.tmp"
        result = _timed_write_case(path, size, mode)
        cases[mode] = result
        report.say(
            f"  {mode:<36} prepare {result['prepare_s']:.3f}  write {result['write_s']:.3f}  "
            f"fsync {result['fsync_s']:.3f}  total {result['total_s']:.3f} s"
        )
    zero_fill = cases["seek-past-end-then-write"]["total_s"]
    presize = cases["presize-ftruncate-then-write-at-end"]["total_s"]
    sparse = cases.get("sparse-then-write-at-end", {}).get("total_s")
    matters = zero_fill >= 0.5
    report.say()
    if matters:
        report.say(
            f"Zero-fill MATTERS here: one 1 MB write at the end of a fresh {args.size_gb:g} GiB "
            f"file stalled for {zero_fill:.2f} s while NTFS filled the gap with zeros."
        )
        if sparse is not None:
            fixed = sparse < 0.1
            report.say(
                f"  With FSCTL_SET_SPARSE the same write took {sparse:.3f} s: "
                + ("the fix works." if fixed else "the fix does NOT remove the stall.")
            )
    else:
        report.say(
            f"Zero-fill is negligible here ({zero_fill:.3f} s): log it and move on, no "
            "sparse-file engineering needed."
        )
    if presize >= 0.5:
        report.say(
            f"  Pre-sizing with os.ftruncate cost {presize:.2f} s: the writer must not pre-size "
            "the output file that way on Windows."
        )
    return {
        "filesystem": fs,
        "size_bytes": size,
        "cases": list(cases.values()),
        "zero_fill_matters": matters,
        "sparse_fix_s": sparse,
    }


# --- job 5: asyncio.sleep accuracy -----------------------------------------------------------


async def _measure_sleeps(delays_ms: Sequence[float], samples: int) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for delay in delays_ms:
        got = []
        for _ in range(samples):
            t = time.perf_counter()
            await asyncio.sleep(delay / 1000)
            got.append((time.perf_counter() - t) * 1000)
        out[f"{delay:g}"] = got
    return out


def summarise(samples: Sequence[float]) -> JSONDict:
    q = statistics.quantiles(samples, n=100, method="inclusive")
    return {
        "min": round(min(samples), 3),
        "p5": round(q[4], 3),
        "p50": round(q[49], 3),
        "p95": round(q[94], 3),
        "p99": round(q[98], 3),
        "max": round(max(samples), 3),
        "mean": round(statistics.fmean(samples), 3),
    }


def timer_job(args: argparse.Namespace, report: Report) -> JSONDict:
    # (label, loop factory, use timeBeginPeriod(1))
    variants: list[tuple[str, Callable[[], asyncio.AbstractEventLoop], bool]] = []
    if sys.platform == "win32":
        variants += [
            ("proactor (Windows default)", asyncio.ProactorEventLoop, False),
            ("selector", asyncio.SelectorEventLoop, False),
            ("proactor + timeBeginPeriod(1)", asyncio.ProactorEventLoop, True),
            ("selector + timeBeginPeriod(1)", asyncio.SelectorEventLoop, True),
        ]
    else:
        variants.append(("default", asyncio.new_event_loop, False))
    report.say(
        f"asyncio.sleep accuracy, {args.samples} samples per delay; monotonic clock resolution "
        f"{time.get_clock_info('monotonic').resolution * 1000:.3f} ms"
    )
    raw: dict[str, dict[str, list[float]]] = {}
    summary: dict[str, dict[str, JSONDict]] = {}
    for name, factory, hires in variants:
        with timer_period_1ms(hires), asyncio.Runner(loop_factory=factory) as runner:
            raw[name] = runner.run(_measure_sleeps(args.delays, args.samples))
        summary[name] = {d: summarise(s) for d, s in raw[name].items()}
        report.say(f"  {name}")
        report.say("    requested    p5      p50     p95     p99     max   (achieved ms)")
        for d, s in summary[name].items():
            report.say(
                f"    {float(d):>6.1f}  {s['p5']:>7.2f} {s['p50']:>7.2f} {s['p95']:>7.2f} "
                f"{s['p99']:>7.2f} {s['max']:>7.2f}"
            )
    report.say()
    for name, per_delay in summary.items():
        honoured = [float(d) for d, s in per_delay.items() if s["p95"] - float(d) <= 2.0]
        smallest = min(honoured) if honoured else None
        report.say(
            f"  {name}: smallest requested delay honoured within +2 ms at p95: "
            f"{f'{smallest:g} ms' if smallest is not None else 'none of those tested'}"
        )
    report.say(
        "  Delays the loop cannot honour cannot be emulated as RTT or jitter by asyncio.sleep in "
        "the loopback shaper."
    )
    return {"summary": summary, "samples_ms": raw}


# --- redaction -------------------------------------------------------------------------------

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_CSV_ROW = re.compile(r"^\d+,[a-z][a-z0-9_-]*,\d+,[01]$")


def _walk(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    return obj


def _collect(obj: Any, visit: Callable[[JSONDict], None]) -> None:
    if isinstance(obj, dict):
        visit(obj)
        for v in obj.values():
            _collect(v, visit)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, visit)


_MAC = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
_NETSH_NAME = re.compile(r"^\s*(?:SSID|Profile|All User Profile)\s*:\s*(.+?)\s*$")


def local_wifi_names() -> set[str]:
    """SSIDs of the current Wi-Fi connection and every saved Wi-Fi profile. Nothing here
    records them on purpose, but they can arrive through interface or system metadata, so the
    redactor scrubs them anyway."""
    if sys.platform != "win32":
        return set()
    names: set[str] = set()
    for cmd in (["netsh", "wlan", "show", "interfaces"], ["netsh", "wlan", "show", "profiles"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.stdout.splitlines():
            m = _NETSH_NAME.match(line)
            if m and m.group(1) and m.group(1).lower() not in ("", "<none>"):
                names.add(m.group(1))
    return names


def local_identity() -> tuple[str, set[str], str]:
    """(hostname, Wi-Fi names, username) of this machine, for scrubbing."""
    host = socket.gethostname() or platform.node()
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    return host, local_wifi_names(), user


class Redactor:
    """Makes results committable: public IPs become public-ip-A/B/... (so "these two links
    share a public IP" survives), ISP names become generic labels, the home directory becomes
    ~, the hostname, Wi-Fi SSIDs and username become placeholders, MAC addresses are masked,
    and any other public IPv4 address that is not a known server address becomes x.x.x.x.

    hostname / ssids / username default to this machine's; tests pass them explicitly."""

    def __init__(
        self,
        payloads: Iterable[Any],
        home: str | None = None,
        hostname: str | None = None,
        ssids: Iterable[str] | None = None,
        username: str | None = None,
    ) -> None:
        if hostname is None or ssids is None or username is None:
            detected_host, detected_ssids, detected_user = local_identity()
            hostname = detected_host if hostname is None else hostname
            ssids = detected_ssids if ssids is None else ssids
            username = detected_user if username is None else username
        self.hostname = hostname
        self.ssids = sorted({s for s in ssids if len(s) >= 2}, key=len, reverse=True)
        self.username = username
        self.public: dict[str, str] = {}
        self.isp: dict[str, str] = {}
        self.servers: set[str] = set()
        counters = {"mobile": 0, "home": 0}

        def visit(d: JSONDict) -> None:
            for key in ("public_ips", "public_ip"):
                value = d.get(key)
                values = value.values() if isinstance(value, dict) else as_list(value)
                for ip in values:
                    if isinstance(ip, str) and ip and ip not in self.public:
                        self.public[ip] = f"public-ip-{chr(ord('A') + len(self.public))}"
            for ip in as_list(d.get("server_ips")):
                if isinstance(ip, str):
                    self.servers.add(ip)
            label = d.get("isp")
            if isinstance(label, str) and label and label not in self.isp and "mobile" in d:
                kind = "mobile" if d.get("mobile") else "home"
                letter = chr(ord("A") + counters[kind])
                counters[kind] += 1
                self.isp[label] = (
                    f"mobile carrier {letter}" if kind == "mobile" else f"home ISP {letter}"
                )

        for payload in payloads:
            _collect(payload, visit)
        self.home = home if home is not None else str(Path.home())

    def scrub(self, text: str) -> str:
        for ip, label in self.public.items():
            text = re.sub(rf"(?<![\d.]){re.escape(ip)}(?![\d.])", label, text)
        for name in sorted(self.isp, key=len, reverse=True):
            if len(name) >= 3:
                text = text.replace(name, self.isp[name])
        if self.home and len(self.home) > 3:
            for variant in {self.home, self.home.replace("\\", "/")}:
                text = text.replace(variant, "~")
        for ssid in self.ssids:
            text = text.replace(ssid, "<wifi-ssid>")
        if self.hostname and len(self.hostname) >= 2:
            text = re.sub(rf"(?i)\b{re.escape(self.hostname)}\b", "<hostname>", text)
        if self.username and len(self.username) >= 3:
            text = re.sub(rf"\b{re.escape(self.username)}\b", "<user>", text)
        text = _MAC.sub("xx:xx:xx:xx:xx:xx", text)
        return _IPV4.sub(self._ip_or_mask, text)

    def leaks(self, text: str) -> list[str]:
        """Kinds of sensitive value still present in `text` (the kind only, never the value)."""
        found = []
        if any(re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d.])", text) for ip in self.public):
            found.append("public IP")
        without_labels = re.sub(r"(?:mobile carrier|home ISP) [A-Z]", "", text)
        if any(len(n) >= 3 and n in without_labels for n in self.isp):
            found.append("ISP/carrier name")
        if self.home and len(self.home) > 3 and self.home in text:
            found.append("home directory")
        if any(s in text for s in self.ssids):
            found.append("Wi-Fi SSID")
        if (
            self.hostname
            and len(self.hostname) >= 2
            and re.search(rf"(?i)\b{re.escape(self.hostname)}\b", text)
        ):
            found.append("hostname")
        if (
            self.username
            and len(self.username) >= 3
            and re.search(rf"\b{re.escape(self.username)}\b", text)
        ):
            found.append("username")
        if _MAC.search(text):
            found.append("MAC address")
        return found

    def _ip_or_mask(self, match: re.Match[str]) -> str:
        ip = match.group(0)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        if ip in self.servers or addr.is_private or addr.is_loopback or addr.is_link_local:
            return ip
        return "x.x.x.x"

    def apply(self, payload: Any) -> Any:
        def fix_isp(d: Any) -> Any:
            if isinstance(d, dict):
                return {
                    k: (self.isp.get(v, v) if k == "isp" and isinstance(v, str) else fix_isp(v))
                    for k, v in d.items()
                }
            if isinstance(d, list):
                return [fix_isp(v) for v in d]
            return d

        out = _walk(fix_isp(payload), self.scrub)
        if isinstance(out, dict):
            out["redacted"] = True
        return out


def validate_trace_csv(text: str) -> None:
    lines = text.splitlines()
    if not lines or lines[0] != "t_ms,link,bytes,saturated":
        raise ValueError("trace CSV header must be t_ms,link,bytes,saturated")
    for n, line in enumerate(lines[1:], start=2):
        if not _CSV_ROW.match(line):
            raise ValueError(f"trace CSV line {n} is not t_ms,link,bytes,saturated: {line[:60]!r}")


class RedactionLeak(RuntimeError):
    """A redacted output still contains a sensitive value; nothing was written."""


def build_redacted(
    payloads: dict[str, Any], csvs: dict[str, str], redactor: Redactor
) -> dict[str, str]:
    """Redacted file contents keyed by relative output path. Fails closed: every output is
    scanned for every sensitive value, and if anything survived, RedactionLeak is raised
    before a single file is written. Pure apart from the redactor, so it is unit-tested."""
    outputs: dict[str, str] = {}
    report_lines = ["# Feasibility results (redacted)", ""]
    for name, payload in payloads.items():
        clean = redactor.apply(payload)
        folder = "scenarios/recorded" if name.endswith(".meta.json") else "docs/feasibility"
        outputs[f"{folder}/{name}"] = json.dumps(clean, indent=2) + "\n"
        if isinstance(clean, dict) and clean.get("report"):
            stem = name.removesuffix(".json")
            report_lines += [f"## {stem}", "", "```", *clean["report"], "```", ""]
    for name, text in csvs.items():
        validate_trace_csv(text)
        outputs[f"scenarios/recorded/{name}"] = text
    if len(report_lines) > 2:
        outputs["docs/feasibility/REPORT.md"] = "\n".join(report_lines)

    problems = {path: kinds for path, text in outputs.items() if (kinds := redactor.leaks(text))}
    if problems:
        detail = "; ".join(f"{path}: {', '.join(kinds)}" for path, kinds in problems.items())
        raise RedactionLeak(f"Redaction incomplete, nothing written. Still present: {detail}")
    return outputs


def redact_outputs(src: Path, repo_root: Path) -> list[Path]:
    json_files = sorted(p for p in src.glob("*.json") if p.name != "throughput-latest.json")
    meta_files = sorted((src / "traces").glob("*.meta.json"))
    csv_files = sorted((src / "traces").glob("*.csv"))
    payloads = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in json_files + meta_files}
    csvs = {p.name: p.read_text(encoding="utf-8") for p in csv_files}
    outputs = build_redacted(payloads, csvs, Redactor(payloads.values()))

    written: list[Path] = []
    for rel, text in outputs.items():
        target = repo_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
        written.append(target)
    return written


def maybe_redact(args: argparse.Namespace) -> None:
    if getattr(args, "redact", False):
        do_redact(Path(args.out), Path(args.redact_to))


def do_redact(src: Path, dest_root: Path) -> None:
    try:
        written = redact_outputs(src, dest_root)
    except RedactionLeak as exc:
        raise SystemExit(f"ERROR: {exc}") from None
    print("\nCommittable copies written (read them before `git add`):", flush=True)
    for path in written:
        shown = path.relative_to(dest_root) if path.is_relative_to(dest_root) else path
        print(f"  {shown}", flush=True)


# --- commands ---------------------------------------------------------------------------------


def cmd_interfaces(args: argparse.Namespace) -> int:
    report = Report()
    infos, facts = gather_interfaces()
    report.say("Interfaces with an IPv4 address (NIC speed is the negotiated rate: display only)")
    report.say(f"  {'name':<28} {'kind':<9} {'up':<5} {'ipv4':<17} {'gateway':<15} weakhostsend")
    for info in infos:
        report.say(
            f"  {info.name:<28} {info.kind:<9} {info.is_up!s:<5} {', '.join(info.ipv4):<17} "
            f"{info.gateway or '-':<15} {info.weak_host_send}"
        )
    usable = [i for i in infos if i.is_up and i.usable_ips and i.kind != "virtual"]
    report.say()
    counts: dict[str, int] = {}
    flags: list[str] = []
    for info in usable:
        prefix = {"wifi": "wifi", "ethernet": "eth", "mobile": "mobile"}.get(info.kind, "link")
        link_id = f"{prefix}{counts.get(prefix, 0)}"
        counts[prefix] = counts.get(prefix, 0) + 1
        flags.append(f'--link {link_id}="{info.name}"')
        if info.kind == "mobile":
            flags.append(f"--mobile {link_id}")
    if flags:
        report.say("Suggested flags (check them; the kind is a guess from the adapter name):")
        report.say("  " + " ".join(flags))
    if len(usable) < 2:
        report.warn(
            f"Only {len(usable)} usable interface(s). Connect Ethernet and/or enable USB "
            "tethering, then run this again."
        )
    links = [
        LinkSpec(i.name.lower(), i.name, i.usable_ips[0], i.kind == "mobile", None, i.kind)
        for i in usable
    ]
    report.say()
    preflight(links, infos, facts, report)
    out = Path(args.out)
    results = {"interfaces": [i.to_json() for i in infos]}
    write_json(
        out / "interfaces.json",
        envelope("interfaces", now_iso(), {}, results, report, timer=timer_environment()),
    )
    maybe_redact(args)
    return 0


def cmd_throughput(args: argparse.Namespace) -> int:
    report = Report()
    infos, facts = gather_interfaces()
    links = parse_links(args, infos)
    require_budget(args, links)
    preflight(links, infos, facts, report)
    started = now_iso()
    with timer_period_1ms(True):  # 1 ms timers instead of 15.6 ms ticks (see the timer job)
        timer = timer_environment()
        results = asyncio.run(throughput_async(args, links, report))
    params = {
        "url": args.url,
        "connections_per_link": args.conns,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "budget_mb": args.budget_mb,
        "go_ratio": GO_RATIO,
    }
    payload = envelope("throughput", started, params, results, report, timer=timer)
    out = Path(args.out)
    write_json(out / f"throughput-{stamp()}.json", payload)
    write_json(out / "throughput-latest.json", payload)
    append_ledger(out, "throughput", int(results["mobile_bytes"]), args.budget_mb)
    maybe_redact(args)
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    for name in ("warmup_s", "burst_s", "idle_s"):
        if not is_bin_multiple(getattr(args, name)):
            raise SystemExit(f"--{name.replace('_', '-')} must be a multiple of {BIN_MS} ms")
    if not re.match(r"^[a-z0-9-]+$", args.session):
        raise SystemExit("--session must be lowercase letters, digits or '-', e.g. am or pm")
    report = Report()
    infos, facts = gather_interfaces()
    links = parse_links(args, infos)
    require_budget(args, links)
    preflight(links, infos, facts, report)
    out = Path(args.out)

    period = args.warmup_s + args.burst_s + args.idle_s
    cycles = int(args.minutes * 60 // period)
    loaded = cycles * (args.warmup_s + args.burst_s)
    projected = project_trace_mobile_bytes(out, links, loaded)
    report.say(
        f"Trace session '{args.session}': {cycles} cycles of {args.warmup_s:g} s warm-up + "
        f"{args.burst_s:g} s saturated + {args.idle_s:g} s idle, {args.conns} connections per link"
    )
    if projected is None:
        report.say("  No mobile projection available (run `throughput` first to get one).")
    else:
        report.say(f"  Projected mobile use: {projected / MB:.0f} MB (budget {args.budget_mb} MB)")
        if (
            args.budget_mb is not None
            and projected > args.budget_mb * MB
            and not args.allow_truncate
        ):
            raise SystemExit(
                "Projected mobile use exceeds --budget-mb, so the session would be cut short. "
                "Lower --minutes or --conns, raise --budget-mb, or pass --allow-truncate."
            )

    state = TraceState(
        links=links, budget=MobileBudget(args.budget_mb, [lk.id for lk in links if lk.mobile])
    )
    exit_code = 0
    try:
        with timer_period_1ms(True):  # cycles start on time, not on a 15.6 ms tick
            state.timer = timer_environment()
            asyncio.run(trace_async(args, state, report))
    except KeyboardInterrupt:
        state.truncated = "interrupted"
        report.warn("Interrupted: writing what was recorded so far.")
        exit_code = 130
    if state.bins is None:
        report.warn("Nothing was recorded.")
        return exit_code or 1
    state.warmup = warmup_check(state.bins.data, state.windows_ms, [lk.id for lk in links])
    report_warmup(state.warmup, args.warmup_s, report)
    connection_reuse = {
        link.id: {
            "requests": state.obs[link.id].requests,
            "distinct_local_ports": len(state.obs[link.id].local_ports),
        }
        for link in links
        if link.id in state.obs
    }
    state.connection_reuse = connection_reuse
    for link_id, c in connection_reuse.items():
        report.say(
            f"  {link_id}: {c['requests']} requests on {c['distinct_local_ports']} distinct local "
            "ports (equal means every request opened a new connection)"
        )
    csv_path, meta_path = write_trace(out, args, state)
    report.say(f"Wrote {csv_path.name} and {meta_path.name} in {csv_path.parent}")
    params = {
        "url": args.url,
        "session": args.session,
        "minutes": args.minutes,
        "connections_per_link": args.conns,
        "budget_mb": args.budget_mb,
    }
    results = {
        "csv": csv_path.name,
        "meta": meta_path.name,
        "truncated": state.truncated,
        "mobile_bytes": state.budget.used,
        "links": [link.to_json() for link in links],
        "public_ips": state.public_ips,
        "warmup_check": state.warmup,
        "connection_reuse": state.connection_reuse,
    }
    write_json(
        out / f"trace-{args.session}-{stamp()}.json",
        envelope("trace", state.started_at, params, results, report, timer=state.timer),
    )
    append_ledger(out, f"trace:{args.session}", state.budget.used, args.budget_mb)
    maybe_redact(args)
    return exit_code


def cmd_loopback(args: argparse.Namespace) -> int:
    report = Report()
    started = now_iso()
    timer = timer_environment()
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_serve"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        port = int(proc.stdout.readline().strip())
        report.say(
            f"Loopback ceiling: server in a separate process (pid {proc.pid}), "
            f"{args.seconds:g} s per run after {args.warmup:g} s warm-up, plain HTTP"
        )
        results = asyncio.run(loopback_async(args, port, proc.pid, report))
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    params = {"seconds": args.seconds, "warmup": args.warmup, "conns_list": args.conns_list}
    write_json(
        Path(args.out) / "loopback.json",
        envelope("loopback", started, params, results, report, timer=timer),
    )
    maybe_redact(args)
    return 0


def cmd_zerofill(args: argparse.Namespace) -> int:
    report = Report()
    started = now_iso()
    timer = timer_environment()
    results = zerofill_job(args, report)
    params = {"dir": str(Path(args.dir).resolve()), "size_gb": args.size_gb}
    write_json(
        Path(args.out) / "zerofill.json",
        envelope("zerofill", started, params, results, report, timer=timer),
    )
    maybe_redact(args)
    return 0


def cmd_timer(args: argparse.Namespace) -> int:
    report = Report()
    started = now_iso()
    timer = timer_environment()
    results = timer_job(args, report)
    params = {"delays_ms": args.delays, "samples": args.samples}
    write_json(
        Path(args.out) / "timer.json",
        envelope("timer", started, params, results, report, timer=timer),
    )
    maybe_redact(args)
    return 0


def cmd_redact(args: argparse.Namespace) -> int:
    do_redact(Path(args.out), Path(args.redact_to))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    asyncio.run(_serve(args.port))
    return 0


def _csv_floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def _csv_ints(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--out", default=str(DEFAULT_OUT), help="raw output directory (git-ignored)"
    )
    common.add_argument(
        "--redact",
        action="store_true",
        help="also write committable copies to docs/feasibility/ and scenarios/recorded/",
    )
    common.add_argument(
        "--redact-to",
        default=str(REPO_ROOT),
        help="root under which redacted copies are written (default: the repo root)",
    )

    net = argparse.ArgumentParser(add_help=False)
    net.add_argument("--link", action="append", metavar="ID=INTERFACE", help="repeat per link")
    net.add_argument("--mobile", action="append", metavar="ID", help="mark a link as mobile data")
    net.add_argument("--isp", action="append", metavar="ID=NAME", help="ISP/carrier label")
    net.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=(
            f"large file to download (default {DEFAULT_URL}; "
            f"alternates: {', '.join(ALTERNATE_URLS)})"
        ),
    )
    net.add_argument("--conns", type=int, default=6, help="connections per link (default 6)")
    net.add_argument("--budget-mb", type=float, default=None, help="hard cap on mobile-link bytes")
    net.add_argument("--no-public-ip", action="store_true", help=f"skip the {PUBLIC_IP_URL} lookup")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("interfaces", parents=[common], help="list adapters and binding pitfalls")
    p.set_defaults(func=cmd_interfaces)

    p = sub.add_parser("throughput", parents=[common, net], help="job 1: solo/combined + verdict")
    p.add_argument("--duration", type=float, default=12.0, help="seconds per phase (default 12)")
    p.add_argument(
        "--warmup", type=float, default=3.0, help="seconds discarded per phase (default 3)"
    )
    p.set_defaults(func=cmd_throughput)

    p = sub.add_parser("trace", parents=[common, net], help="job 2: duty-cycled trace recorder")
    p.add_argument("--session", required=True, help="session label, e.g. am or pm")
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--burst-s", type=float, default=5.0, help="saturated seconds per cycle")
    p.add_argument("--idle-s", type=float, default=24.0, help="idle seconds per cycle")
    p.add_argument(
        "--warmup-s",
        type=float,
        default=1.0,
        help="seconds of load before each saturated window, recorded with saturated=0",
    )
    p.add_argument("--conditions", default="", help="free-text note: place, weather, signal, ...")
    p.add_argument("--allow-truncate", action="store_true")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("loopback", parents=[common], help="job 3: Python/httpx ceiling")
    p.add_argument("--seconds", type=float, default=4.0)
    p.add_argument("--warmup", type=float, default=1.0)
    p.add_argument("--conns-list", type=_csv_ints, default=[1, 3, 9])
    p.set_defaults(func=cmd_loopback)

    p = sub.add_parser("zerofill", parents=[common], help="job 4: NTFS zero-fill cost")
    p.add_argument(
        "--dir", default=str(DEFAULT_OUT / "zerofill"), help="put this on your download drive"
    )
    p.add_argument("--size-gb", type=float, default=4.0)
    p.set_defaults(func=cmd_zerofill)

    p = sub.add_parser("timer", parents=[common], help="job 5: asyncio.sleep accuracy")
    p.add_argument("--delays", type=_csv_floats, default=[1.0, 5.0, 15.0, 20.0, 50.0])
    p.add_argument("--samples", type=int, default=100)
    p.set_defaults(func=cmd_timer)

    p = sub.add_parser("redact", parents=[common], help="write committable copies of --out")
    p.set_defaults(func=cmd_redact)

    p = sub.add_parser("_serve")  # internal: the loopback server process
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
