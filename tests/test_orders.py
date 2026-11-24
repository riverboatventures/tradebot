from datetime import datetime, timedelta

import pytest
from conftest import NY, proposal

from tradebot.brokers.ninjatrader import NinjaTraderBroker
from tradebot.models import Position
from tradebot.orders import OrderTracker

T0 = datetime(2026, 11, 24, 11, 0, tzinfo=NY)


@pytest.fixture
def nt(tmp_path):
    (tmp_path / "incoming").mkdir()
    (tmp_path / "outgoing").mkdir()
    return NinjaTraderBroker(False, False, "Sim101", str(tmp_path), {"MES": "12-26", "MCL": "01-27"})


def oifs(nt):
    """All instruction lines written since the last call (and clear them)."""
    lines = []
    for f in sorted(nt.incoming.glob("oif*.txt")):
        lines += f.read_text().splitlines()
        f.unlink()
    return lines


def state(nt, oid, s, filled=0):
    (nt.outgoing / f"{oid}.txt").write_text(f"{s};{filled};0")


def place(nt, tmp_path, alerts):
    tr = OrderTracker(nt, tmp_path / "orders.json", alert=lambda lvl, m: alerts.append((lvl, m)))
    res = nt.place_bracket(proposal(quantity=2))
    oifs(nt)
    return tr, tr.track(proposal(quantity=2), res, T0)


def test_healthy_bracket_no_action(nt, tmp_path):
    alerts = []
    tr, b = place(nt, tmp_path, alerts)
    state(nt, b.entry_id, "Filled", 2); state(nt, b.stop_id, "Accepted"); state(nt, b.target_id, "Working")
    tr.reconcile([Position(symbol="MES", quantity=2, avg_price=6000)], T0 + timedelta(minutes=5))
    assert oifs(nt) == [] and alerts == [] and b.status == "open"


def test_rejected_entry_cancels_exits(nt, tmp_path):
    alerts = []
    tr, b = place(nt, tmp_path, alerts)
    state(nt, b.entry_id, "Rejected")
    tr.reconcile([], T0 + timedelta(seconds=5))
    lines = oifs(nt)
    assert any(b.stop_id in l and l.startswith("CANCEL") for l in lines)
    assert any(b.target_id in l and l.startswith("CANCEL") for l in lines)
    assert b.status == "failed"


def test_missing_stop_closes_position(nt, tmp_path):
    alerts = []
    tr, b = place(nt, tmp_path, alerts)
    state(nt, b.entry_id, "Filled", 2); state(nt, b.stop_id, "Rejected"); state(nt, b.target_id, "Working")
    pos = [Position(symbol="MES", quantity=2, avg_price=6000)]
    tr.reconcile(pos, T0 + timedelta(seconds=5))   # within grace: wait
    assert oifs(nt) == []
    tr.reconcile(pos, T0 + timedelta(seconds=30))
    assert oifs(nt) == ["CLOSEPOSITION;Sim101;MES 12-26;DAY"]
    assert alerts[-1][0] == "critical"


def test_closed_position_cancels_leftover_exit(nt, tmp_path):
    alerts = []
    tr, b = place(nt, tmp_path, alerts)
    state(nt, b.entry_id, "Filled", 2); state(nt, b.stop_id, "Accepted"); state(nt, b.target_id, "Working")
    tr.reconcile([Position(symbol="MES", quantity=2, avg_price=6000)], T0 + timedelta(minutes=1))
    state(nt, b.target_id, "Filled", 2)  # target hit, but OCO failed to cancel the stop
    tr.reconcile([], T0 + timedelta(minutes=30))
    assert any(l.startswith("CANCEL") and b.stop_id in l for l in oifs(nt))
    assert b.status == "closed"


def test_oversized_position_closed(nt, tmp_path):
    alerts = []
    tr, b = place(nt, tmp_path, alerts)
    state(nt, b.entry_id, "Filled", 2); state(nt, b.stop_id, "Accepted")
    tr.reconcile([Position(symbol="MES", quantity=4, avg_price=6000)], T0 + timedelta(seconds=5))
    assert "CLOSEPOSITION;Sim101;MES 12-26;DAY" in oifs(nt)


def test_entry_never_confirmed_times_out(nt, tmp_path):
    alerts = []
    tr, b = place(nt, tmp_path, alerts)  # no state files at all
    tr.reconcile([], T0 + timedelta(seconds=10))
    assert b.status == "pending"
    tr.reconcile([], T0 + timedelta(seconds=90))
    assert b.status == "failed" and len([l for l in oifs(nt) if l.startswith("CANCEL")]) == 3


def test_untracked_position_closed_after_grace(nt, tmp_path):
    tr = OrderTracker(nt, tmp_path / "orders.json", alert=lambda *a: None)
    pos = [Position(symbol="MCL", quantity=-1, avg_price=70)]
    tr.reconcile(pos, T0)
    assert oifs(nt) == []
    tr = OrderTracker(nt, tmp_path / "orders.json", alert=lambda *a: None)  # survives restart
    tr.reconcile(pos, T0 + timedelta(seconds=45))
    assert oifs(nt) == ["CLOSEPOSITION;Sim101;MCL 01-27;DAY"]


def test_tracker_state_survives_restart(nt, tmp_path):
    tr, b = place(nt, tmp_path, [])
    tr2 = OrderTracker(nt, tmp_path / "orders.json")
    assert [x.entry_id for x in tr2.active] == [b.entry_id]


def test_mode_selects_accounts(monkeypatch, tmp_path):
    from tradebot.cli import build_brokers
    from tradebot.config import Settings
    s = Settings.model_validate({"mode": "demo", "brokers": {"ninjatrader": {
        "enabled": True, "nt_folder": str(tmp_path), "accounts": [
            {"name": "Sim101"}, {"name": "LIVE1", "live": True, "bot_only": False}]}}})
    assert [b.name for b in build_brokers(s)] == ["ninjatrader:Sim101"]
    s.mode = "live"
    monkeypatch.delenv("TRADEBOT_LIVE_ARMED", raising=False)
    with pytest.raises(SystemExit):
        build_brokers(s)  # live without arming refuses to start
    monkeypatch.setenv("TRADEBOT_LIVE_ARMED", "yes-real-money")
    brokers = build_brokers(s)
    assert [(b.name, b.live, b.bot_only) for b in brokers] == [
        ("ninjatrader:Sim101", False, True), ("ninjatrader:LIVE1", True, False)]


def test_not_bot_only_alerts_instead_of_closing(nt, tmp_path):
    alerts = []
    tr = OrderTracker(nt, tmp_path / "o.json", alert=lambda lvl, m: alerts.append(m), flatten_untracked=False)
    pos = [Position(symbol="MES", quantity=1, avg_price=6000)]
    tr.reconcile(pos, T0)
    tr.reconcile(pos, T0 + timedelta(minutes=5))
    assert oifs(nt) == [] and len(alerts) == 1
