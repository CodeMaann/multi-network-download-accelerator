"""Tests for the pure parts of scripts/feasibility.py: the parts where a bug would silently
produce a wrong verdict, a wrong trace, or leak a public IP into a commit."""

from __future__ import annotations

import argparse
import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "feasibility.py"
    spec = importlib.util.spec_from_file_location("feasibility", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["feasibility"] = module
    spec.loader.exec_module(module)
    return module


fz: Any = _load()


# --- verdict ------------------------------------------------------------------------------


def verdict(**overrides: Any) -> Any:
    args: dict[str, Any] = {
        "solo_before": {"wifi0": 100.0, "mobile0": 60.0},
        "solo_after": {"wifi0": 100.0, "mobile0": 60.0},
        "combined": {"wifi0": 95.0, "mobile0": 58.0},
        "server_ips": {"wifi0": {"1.1.1.1"}, "mobile0": {"2.2.2.2"}},
        "binding_ok": {"wifi0": True, "mobile0": True},
        "public_ips": {"wifi0": "8.8.8.8", "mobile0": "9.9.9.9"},
    }
    args.update(overrides)
    return fz.compute_verdict(**args)


def test_go_when_combined_reaches_threshold() -> None:
    v = verdict()
    assert v["status"] == "GO"
    assert v["go"] is True
    assert v["best_link"] == "wifi0"
    assert v["gain"] == pytest.approx(153 / 100)
    assert v["suspect"] is False


def test_no_go_from_asymmetry_says_links_do_stack() -> None:
    v = verdict(
        solo_before={"wifi0": 100.0, "mobile0": 20.0},
        solo_after={"wifi0": 100.0, "mobile0": 20.0},
        combined={"wifi0": 98.0, "mobile0": 19.0},
    )
    assert v["status"] == "NO-GO"
    assert any("do stack" in n for n in v["notes"])


def test_no_go_from_interference_says_so() -> None:
    v = verdict(combined={"wifi0": 60.0, "mobile0": 40.0})
    assert v["status"] == "NO-GO"
    assert any("interfere" in n for n in v["notes"])
    assert any("fell to" in n for n in v["notes"])


def test_same_server_makes_a_no_go_suspect() -> None:
    v = verdict(
        combined={"wifi0": 60.0, "mobile0": 40.0},
        server_ips={"wifi0": {"5.5.5.5"}, "mobile0": {"5.5.5.5"}},
    )
    assert v["suspect"] is True
    assert v["status"].startswith("NO-GO (SUSPECT")
    assert any("--url" in r for r in v["reasons"])


def test_same_server_does_not_taint_a_go() -> None:
    # A shared server-side limit can only make links look like they don't stack.
    v = verdict(server_ips={"wifi0": {"5.5.5.5"}, "mobile0": {"5.5.5.5"}})
    assert v["status"] == "GO"
    assert v["suspect"] is False
    assert v["same_server_ip"] is True


def test_binding_failure_is_invalid() -> None:
    v = verdict(binding_ok={"wifi0": True, "mobile0": False})
    assert v["status"] == "INVALID"
    assert v["go"] is None


def test_single_link_is_not_applicable() -> None:
    v = verdict(combined={"wifi0": 100.0})
    assert v["status"] == "N/A"


def test_moderate_drift_and_shared_public_ip_are_noted() -> None:
    v = verdict(
        solo_before={"wifi0": 100.0, "mobile0": 70.0},
        solo_after={"wifi0": 100.0, "mobile0": 52.0},  # 1.35x: drift, not invalid
        combined={"wifi0": 95.0, "mobile0": 55.0},
        public_ips={"wifi0": "8.8.8.8", "mobile0": "8.8.8.8"},
    )
    assert v["status"] == "GO"
    assert any("drifted" in n for n in v["notes"])
    assert any("share one public IP" in n for n in v["notes"])


def test_solo_speeds_differing_by_more_than_1_5x_are_invalid() -> None:
    v = verdict(
        solo_before={"wifi0": 100.0, "mobile0": 80.0},
        solo_after={"wifi0": 100.0, "mobile0": 40.0},
    )
    assert v["status"] == "INVALID"
    assert v["go"] is None
    assert any("2.00x" in r for r in v["reasons"])


def test_efficiency_above_100_percent_is_invalid() -> None:
    v = verdict(combined={"wifi0": 110.0, "mobile0": 60.0})  # 170 > 100 + 60
    assert v["status"] == "INVALID"
    assert v["go"] is None
    assert v["stacking_efficiency"] > 1
    assert any("exceeds the sum" in r for r in v["reasons"])


def test_a_dropout_phase_on_a_live_link_is_invalid() -> None:
    v = verdict(
        solo_before={"wifi0": 100.0, "mobile0": 60.0},
        solo_after={"wifi0": 100.0, "mobile0": 0.0},
        combined={"wifi0": 95.0, "mobile0": 58.0},
    )
    assert v["status"] == "INVALID"
    assert any("solo-after measured 0.00 Mbps, a dropout" in r for r in v["reasons"])


def test_a_dead_link_is_excluded_and_one_live_link_is_a_no_go() -> None:
    # The recorded run: Wi-Fi with a final dropout, a mobile link at 0.4-0.5 Mbps.
    v = verdict(
        solo_before={"wifi0": 31.6, "mobile0": 0.5},
        solo_after={"wifi0": 0.0, "mobile0": 0.4},
        combined={"wifi0": 15.0, "mobile0": 0.4},
    )
    assert v["status"] == "NO-GO"
    assert v["go"] is False
    assert v["dead_links"] == {"mobile0": 0.45}
    assert v["live_links"] == ["wifi0"]
    assert any("nothing to stack" in r for r in v["reasons"])
    assert any("effectively dead" in n for n in v["notes"])
    assert any(n.startswith("Also unstable") and "wifi0" in n for n in v["notes"])
    assert "gain" not in v


def test_stacking_is_judged_on_live_links_only() -> None:
    v = verdict(
        solo_before={"wifi0": 100.0, "eth0": 60.0, "mobile0": 0.4},
        solo_after={"wifi0": 100.0, "eth0": 60.0, "mobile0": 0.0},  # dead, so its dropout
        combined={"wifi0": 95.0, "eth0": 58.0, "mobile0": 0.2},  # does not invalidate
        server_ips={"wifi0": {"1.1.1.1"}, "eth0": {"2.2.2.2"}, "mobile0": {"3.3.3.3"}},
        binding_ok={"wifi0": True, "eth0": True, "mobile0": True},
    )
    assert v["status"] == "GO"
    assert v["live_links"] == ["wifi0", "eth0"]
    assert v["combined_mbps"] == pytest.approx(153.0)
    assert v["stacking_efficiency"] == pytest.approx(153 / 160, abs=1e-3)


def test_a_server_refused_phase_is_invalid_and_not_a_dropout() -> None:
    v = verdict(
        solo_after={"wifi0": 0.0, "mobile0": 60.0},
        refused_s={"wifi0": {"solo-after": 8.0}},
    )
    assert v["status"] == "INVALID"
    assert any("refused every connection for 8.0 s of solo-after" in r for r in v["reasons"])
    assert not any("dropout" in r for r in v["reasons"])


def test_a_link_refused_in_its_solo_phases_is_never_declared_dead() -> None:
    v = verdict(
        solo_before={"wifi0": 100.0, "mobile0": 0.0},
        solo_after={"wifi0": 100.0, "mobile0": 0.0},
        refused_s={"mobile0": {"solo-before": 9.0, "solo-after": 9.0}},
    )
    assert v["dead_links"] == {}
    assert v["status"] == "INVALID"


def test_rejection_backoff() -> None:
    assert fz.rejection_backoff("5", 1) == 5.0
    assert fz.rejection_backoff("600", 1) == fz.REJECTION_BACKOFF_MAX_S
    assert fz.rejection_backoff("Wed, 21 Oct 2026 07:28:00 GMT", 1) == 2.0
    assert fz.rejection_backoff(None, 3) == 8.0
    assert fz.rejection_backoff(None, 10) == fz.REJECTION_BACKOFF_MAX_S


def test_overlap() -> None:
    assert fz.overlap_s([(0.0, 2.0), (5.0, 9.0)], 1.0, 8.0) == pytest.approx(4.0)


def test_public_ips_are_in_the_verdict_even_when_invalid() -> None:
    v = verdict(binding_ok={"wifi0": True, "mobile0": False})
    assert v["status"] == "INVALID"
    assert v["public_ips"] == {"wifi0": "8.8.8.8", "mobile0": "9.9.9.9"}
    assert v["public_ip_sharing"] == {"exact": [], "same_24": []}


def test_public_ip_report_lines() -> None:
    report = fz.Report()
    fz.report_public_ips(
        verdict(public_ips={"wifi0": "198.51.100.23", "mobile0": "198.51.100.99"}),
        enabled=True,
        report=report,
    )
    assert "wifi0=198.51.100.23" in report.lines[0]
    assert "weaker signal" in report.lines[1]
    report = fz.Report()
    fz.report_public_ips(verdict(), enabled=True, report=report)
    assert "no two links share" in report.lines[1]


def test_budget_note_only_when_budget_given(capsys: pytest.CaptureFixture[str]) -> None:
    link = fz.LinkSpec("wifi0", "Wi-Fi", "192.168.1.36", False, None, "wifi")
    fz.require_budget(argparse.Namespace(budget_mb=None), [link])
    assert capsys.readouterr().out == ""
    fz.require_budget(argparse.Namespace(budget_mb=100.0), [link])
    assert "limits nothing" in capsys.readouterr().out


# --- binning, duty cycle, dense rows, budget -------------------------------------------------


def test_bins_assign_by_100ms_and_total_by_window() -> None:
    b = fz.Bins(t0=10.0)
    b.add("a", 10.00, 5)
    b.add("a", 10.099, 7)
    b.add("a", 10.1, 11)
    b.add("a", 11.25, 13)
    assert b.data["a"] == {0: 12, 1: 11, 12: 13}
    assert b.total("a") == 36
    assert b.total("a", 0.1, 1.2) == 11
    assert b.max_index == 12


def test_saturated_flags_only_cover_whole_bins_inside_windows() -> None:
    flags = fz.saturated_flags(12, [(200, 500), (900, 1000)])
    assert flags == [0, 0, 1, 1, 1, 0, 0, 0, 0, 1, 0, 0]


def test_bin_rounding() -> None:
    assert fz.ceil_bin_ms(1.0) == 1000
    assert fz.ceil_bin_ms(1.0001) == 1100
    assert fz.floor_bin_ms(6.0999) == 6000
    assert fz.is_bin_multiple(0.3)
    assert not fz.is_bin_multiple(0.25)


def test_trace_rows_are_dense_with_zeros() -> None:
    rows = fz.trace_rows(["wifi0", "mobile0"], {"wifi0": {1: 500}}, 3, [0, 1, 0])
    assert rows == [
        (0, "wifi0", 0, 0),
        (0, "mobile0", 0, 0),
        (100, "wifi0", 500, 1),
        (100, "mobile0", 0, 1),
        (200, "wifi0", 0, 0),
        (200, "mobile0", 0, 0),
    ]


def test_unmeasured_bins_are_missing_not_zero() -> None:
    rows = fz.trace_rows(
        ["wifi0", "mobile0"], {"wifi0": {1: 500}}, 3, [1, 1, 1], {"mobile0": {1, 2}}
    )
    assert rows[3] == (100, "mobile0", None, 1)
    assert rows[1] == (0, "mobile0", 0, 1)  # measured zero stays zero
    assert rows[2] == (100, "wifi0", 500, 1)


def test_unmeasured_bins_cover_every_overlapped_bin() -> None:
    assert fz.unmeasured_bins([(0.25, 0.31)]) == {2, 3}
    assert fz.unmeasured_bins([(0.3, 0.3)]) == {3}
    assert fz.unmeasured_bins([(0.0, 0.1), (1.05, 1.2)]) == {0, 10, 11}


def test_dropout_summary_separates_outages_from_packet_timing() -> None:
    sat = [(k * 100, "m", 0 if 5 <= k < 17 else (None if k == 20 else 900), 1) for k in range(25)]
    rows = [*sat, (2500, "m", 0, 0), (2600, "m", 0, 1)]  # an unsaturated bin breaks a run
    d = fz.dropout_summary(rows, ["m"])["m"]
    assert d["saturated_bins"] == 26
    assert d["unmeasured_bins"] == 1
    assert d["zero_byte_bins"] == 13
    assert d["outages_1s_or_longer"] == 1
    assert d["outage_seconds"] == pytest.approx(1.2)
    assert d["longest_zero_run_s"] == pytest.approx(1.2)


def test_error_classes() -> None:
    def chained(outer: BaseException, cause: BaseException) -> BaseException:
        outer.__cause__ = cause
        return outer

    assert fz.error_class(fz.httpx.ReadTimeout("t")) == "link"
    assert fz.error_class(fz.httpx.RemoteProtocolError("peer closed")) == "link"
    assert fz.error_class(chained(fz.httpx.ConnectError("x"), OSError(10051, "unreachable"))) == (
        "link"
    )
    dns = chained(fz.httpx.ConnectError("x"), socket.gaierror(11001, "getaddrinfo failed"))
    assert fz.error_class(dns) == "measurement"
    refused = chained(
        fz.httpx.ConnectError("x"),
        chained(OSError("All connection attempts failed"), ConnectionRefusedError()),
    )
    assert fz.error_class(refused) == "measurement"
    assert fz.error_class(fz.httpx.DecodingError("x")) == "measurement"


def test_unmeasured_only_while_every_connection_is_in_a_measurement_fault() -> None:
    um = fz.Unmeasured(t0=100.0, conns=2)
    um.set_fault("m", False, True, 101.0)
    assert um.intervals == {}
    um.set_fault("m", False, True, 102.0)  # now both connections faulted
    um.set_fault("m", True, True, 102.5)  # no change
    um.set_fault("m", True, False, 103.0)  # one recovers: interval closes
    um.set_fault("m", True, False, 104.0)
    assert um.intervals == {"m": [(2.0, 3.0)]}
    assert um.faulted["m"] == 0


def _trace_args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "minutes": 20.0,
        "mode": "continuous",
        "warmup_s": None,
        "burst_s": None,
        "idle_s": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_continuous_mode_is_one_window() -> None:
    args = _trace_args()
    fz.resolve_trace_timing(args)
    assert (args.warmup_s, args.burst_s, args.idle_s) == (10.0, 1190.0, 0.0)
    period = args.warmup_s + args.burst_s + args.idle_s
    assert int(round(args.minutes * 60, 6) // period) == 1


def test_trace_timing_rejects_misleading_flags() -> None:
    with pytest.raises(SystemExit, match="duty-cycled only"):
        fz.resolve_trace_timing(_trace_args(idle_s=0.0))
    with pytest.raises(SystemExit, match="--mode continuous"):
        fz.resolve_trace_timing(_trace_args(mode="duty-cycled", idle_s=0.0))
    args = _trace_args(mode="duty-cycled", minutes=10.0)
    fz.resolve_trace_timing(args)
    assert (args.warmup_s, args.burst_s, args.idle_s) == (1.0, 5.0, 24.0)


def test_budget_counts_mobile_only_and_trips_at_limit() -> None:
    budget = fz.MobileBudget(1.0, ["mobile0"])  # 1 MB = 10^6 bytes
    budget.charge("wifi0", 5_000_000)
    assert budget.used == 0
    budget.charge("mobile0", 999_999)
    assert not budget.exhausted.is_set()
    budget.charge("mobile0", 1)
    assert budget.exhausted.is_set()


# --- redaction ------------------------------------------------------------------------------


def _payloads() -> list[Any]:
    return [
        {
            "results": {
                "links": [
                    {
                        "id": "wifi0",
                        "isp": "Acme Fibre",
                        "mobile": False,
                        "local_ip": "192.168.1.36",
                    },
                    {"id": "mobile0", "isp": "CellCo", "mobile": True, "local_ip": "10.24.5.9"},
                ],
                "public_ips": {"wifi0": "203.0.113.7", "mobile0": "198.51.100.23"},
                "observed": {"wifi0": {"server_ips": ["5.223.7.195"]}},
            },
            "report": [
                "wifi0 public 203.0.113.7 via Acme Fibre, server 5.223.7.195",
                "error from 198.51.100.23 and stray 100.1.2.3; file C:\\Users\\someone\\x",
            ],
        }
    ]


def _redactor(payloads: list[Any], **overrides: Any) -> Any:
    identity: dict[str, Any] = {
        "home": "C:\\Users\\someone",
        "hostname": "LAPTOP-QX7Z",
        "ssids": ["Sharma Home 5G", "CafeNet"],
        "username": "someone",
    }
    identity.update(overrides)
    return fz.Redactor(payloads, **identity)


def test_redaction_removes_public_ips_and_isp_names() -> None:
    payloads = _payloads()
    r = _redactor(payloads)
    out = r.apply(payloads[0])
    text = repr(out)
    assert "203.0.113.7" not in text
    assert "198.51.100.23" not in text
    assert "100.1.2.3" not in text
    assert "Acme Fibre" not in text
    assert "CellCo" not in text
    assert "someone" not in text
    assert out["results"]["public_ips"] == {"wifi0": "public-ip-A", "mobile0": "public-ip-B"}
    links = out["results"]["links"]
    assert links[0]["isp"] == "home ISP A"
    assert links[1]["isp"] == "mobile carrier A"
    assert out["redacted"] is True


def test_redaction_keeps_private_and_server_addresses() -> None:
    payloads = _payloads()
    out = _redactor(payloads).apply(payloads[0])
    assert out["results"]["links"][0]["local_ip"] == "192.168.1.36"
    assert out["results"]["links"][1]["local_ip"] == "10.24.5.9"
    assert "5.223.7.195" in out["report"][0]
    assert "x.x.x.x" in out["report"][1]


def test_redaction_preserves_equal_public_ips() -> None:
    payloads = [{"public_ips": {"wifi0": "203.0.113.7", "eth0": "203.0.113.7"}}]
    out = _redactor(payloads, home="").apply(payloads[0])
    assert out["public_ips"] == {"wifi0": "public-ip-A", "eth0": "public-ip-A"}


def test_redaction_removes_hostname_ssid_username_and_mac() -> None:
    payloads: list[Any] = [
        {
            "report": [
                "host laptop-qx7z joined Sharma Home 5G (bssid a4:2b:b0:12:9F:0e)",
                "profile CafeNet, user someone, adapter 00-1A-2B-3C-4D-5E",
                "someoneelse and handsomeone are other words",
            ]
        }
    ]
    out = _redactor(payloads).apply(payloads[0])
    text = "\n".join(out["report"])
    for secret in ("laptop-qx7z", "Sharma Home 5G", "CafeNet", "a4:2b", "00-1A"):
        assert secret.lower() not in text.lower()
    assert "<hostname>" in text
    assert text.count("<wifi-ssid>") == 2
    assert "user <user>," in text
    assert "someoneelse and handsomeone" in text  # whole words only
    assert text.count("xx:xx:xx:xx:xx:xx") == 2


def test_build_redacted_fails_closed_on_a_surviving_secret() -> None:
    # Dict keys are not rewritten, so a secret used as a key survives scrubbing; the final
    # scan must catch it and refuse to produce any output.
    payloads = {"leaky.json": {"by_host": {"LAPTOP-QX7Z": 1}}}
    with pytest.raises(fz.RedactionLeak, match="hostname"):
        fz.build_redacted(payloads, {}, _redactor(list(payloads.values())))


def test_build_redacted_routes_files_and_validates_csv() -> None:
    payloads = {
        "throughput-1.json": {"report": ["ok 203.0.113.7"], "public_ips": {"a": "203.0.113.7"}},
        "mobile-x-am.meta.json": {"public_ips": {"a": "203.0.113.7"}},
    }
    csvs = {"mobile-x-am.csv": "t_ms,link,bytes,saturated\n0,a,0,0\n"}
    out = fz.build_redacted(payloads, csvs, _redactor(list(payloads.values())))
    assert set(out) == {
        "docs/feasibility/throughput-1.json",
        "docs/feasibility/REPORT.md",
        "scenarios/recorded/mobile-x-am.meta.json",
        "scenarios/recorded/mobile-x-am.csv",
    }
    assert all("203.0.113.7" not in text for text in out.values())


# --- public-IP sharing, warm-up check, timer period -------------------------------------------


def test_public_ip_sharing_exact_and_same_24() -> None:
    sharing = fz.public_ip_sharing(
        {
            "wifi0": "203.0.113.7",
            "eth0": "203.0.113.7",
            "mobile0": "198.51.100.23",
            "mobile1": "198.51.100.200",
            "none": None,
        }
    )
    assert sharing["exact"] == [["eth0", "wifi0"]]
    assert sharing["same_24"] == [{"links": ["mobile0", "mobile1"], "prefix": "198.51.100.0/24"}]


def test_verdict_labels_same_24_as_weaker_signal() -> None:
    v = verdict(public_ips={"wifi0": "198.51.100.23", "mobile0": "198.51.100.99"})
    assert any(n.startswith("WEAKER SIGNAL") and "/24" in n for n in v["notes"])
    assert not any("share one public IP" in n for n in v["notes"])


def _window_bins(start_bin: int, values: list[int]) -> dict[int, int]:
    return {start_bin + k: v for k, v in enumerate(values)}


def test_warmup_check_flags_a_ramping_first_bin() -> None:
    # Two 500 ms saturated windows at 8 Mbps (100 KB per bin); "slow" starts each at half rate.
    data = {
        "slow": {
            **_window_bins(10, [50_000, 100_000, 100_000, 100_000, 100_000]),
            **_window_bins(30, [50_000, 100_000, 100_000, 100_000, 100_000]),
        },
        "fine": {
            **_window_bins(10, [98_000, 100_000, 100_000, 100_000, 100_000]),
            **_window_bins(30, [101_000, 100_000, 100_000, 100_000, 100_000]),
        },
    }
    check = fz.warmup_check(data, [(1000, 1500), (3000, 3500)], ["slow", "fine", "absent"])
    assert check["status"] == "insufficient"
    assert check["links"]["slow"]["status"] == "insufficient"
    assert check["links"]["slow"]["first_to_later_ratio"] == pytest.approx(0.5)
    assert check["links"]["slow"]["ratio_by_position"][0] == pytest.approx(0.5)
    assert check["links"]["fine"]["status"] == "ok"
    assert check["links"]["absent"]["status"] == "n/a"


def test_warmup_check_refuses_a_ratio_on_a_slow_or_noisy_link() -> None:
    noisy = [100_000, 50_000, 150_000] * 10  # one window, CV 0.4
    data = {
        "mobile0": _window_bins(0, [6000, 0, 12_000, 6000, 0, 6000, 12_000, 6000]),  # ~0.5 Mbps
        "wifi0": _window_bins(0, [100_000, *noisy]),
        "ramping": _window_bins(0, [0, *noisy]),  # low even allowing for the noise
    }
    check = fz.warmup_check(data, [(0, 3100)], ["mobile0", "wifi0", "ramping"])
    links = check["links"]
    assert links["mobile0"]["status"] == "too_slow"
    assert links["wifi0"]["status"] == "inconclusive"
    assert links["wifi0"]["ratio_standard_error"] > 0.075
    assert links["ramping"]["status"] == "insufficient"

    report = fz.Report()
    fz.report_warmup(check, 10.0, report)
    text = "\n".join(report.lines)
    assert "mobile0: NOT CHECKED, too slow" in text
    assert "wifi0: NOT CHECKED, bin-to-bin noise" in text
    assert "= 0.4" not in text  # no ratio printed for links it can't judge


def test_warmup_overall_status_when_only_some_links_can_be_judged() -> None:
    data = {
        "wifi0": _window_bins(0, [100_000] * 10),
        "mobile0": _window_bins(0, [6000] * 10),
    }
    assert fz.warmup_check(data, [(0, 1000)], ["wifi0", "mobile0"])["status"] == (
        "ok_where_meaningful"
    )
    assert fz.warmup_check(data, [(0, 1000)], ["mobile0"])["status"] == "not_meaningful"


@pytest.mark.skipif(sys.platform != "win32", reason="timeBeginPeriod is Windows-only")
def test_timer_period_is_released_even_when_the_block_raises() -> None:
    assert fz._active_timer_period_ms is None
    with pytest.raises(ValueError), fz.timer_period_1ms():
        assert fz._active_timer_period_ms == 1
        raise ValueError("boom")
    assert fz._active_timer_period_ms is None


def test_trace_csv_validation() -> None:
    fz.validate_trace_csv("t_ms,link,bytes,saturated\n0,wifi0,0,0\n100,mobile0,52000,1\n")
    fz.validate_trace_csv("t_ms,link,bytes,saturated\n0,mobile0,,1\n")  # unmeasured
    with pytest.raises(ValueError, match="header"):
        fz.validate_trace_csv("t_ms,link,bytes\n0,wifi0,0\n")
    with pytest.raises(ValueError, match="line 2"):
        fz.validate_trace_csv("t_ms,link,bytes,saturated\n0,203.0.113.7,0,0\n")
