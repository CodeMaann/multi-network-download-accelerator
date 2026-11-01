"""Tests for the pure parts of scripts/feasibility.py: the parts where a bug would silently
produce a wrong verdict, a wrong trace, or leak a public IP into a commit."""

from __future__ import annotations

import importlib.util
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


def test_drift_and_shared_public_ip_are_noted() -> None:
    v = verdict(
        solo_before={"wifi0": 100.0, "mobile0": 80.0},
        solo_after={"wifi0": 100.0, "mobile0": 40.0},
        public_ips={"wifi0": "8.8.8.8", "mobile0": "8.8.8.8"},
    )
    assert any("drifted" in n for n in v["notes"])
    assert any("share one public IP" in n for n in v["notes"])


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


def test_redaction_removes_public_ips_and_isp_names() -> None:
    payloads = _payloads()
    r = fz.Redactor(payloads, home="C:\\Users\\someone")
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
    out = fz.Redactor(payloads, home="C:\\Users\\someone").apply(payloads[0])
    assert out["results"]["links"][0]["local_ip"] == "192.168.1.36"
    assert out["results"]["links"][1]["local_ip"] == "10.24.5.9"
    assert "5.223.7.195" in out["report"][0]
    assert "x.x.x.x" in out["report"][1]


def test_redaction_preserves_equal_public_ips() -> None:
    payloads = [{"public_ips": {"wifi0": "203.0.113.7", "eth0": "203.0.113.7"}}]
    out = fz.Redactor(payloads, home="").apply(payloads[0])
    assert out["public_ips"] == {"wifi0": "public-ip-A", "eth0": "public-ip-A"}


def test_trace_csv_validation() -> None:
    fz.validate_trace_csv("t_ms,link,bytes,saturated\n0,wifi0,0,0\n100,mobile0,52000,1\n")
    with pytest.raises(ValueError, match="header"):
        fz.validate_trace_csv("t_ms,link,bytes\n0,wifi0,0\n")
    with pytest.raises(ValueError, match="line 2"):
        fz.validate_trace_csv("t_ms,link,bytes,saturated\n0,203.0.113.7,0,0\n")
