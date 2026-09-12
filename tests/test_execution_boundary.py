import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from test_unified_backtest import build, tape_for

from kiwit.brokers.boundary import DisabledGrowwLiveAdapter, PaperExecutionBoundary


def approved(tmp_path):
    core, snapshot = build(tmp_path)
    tape, _ = tape_for(core, snapshot)
    captured = []
    original = core.execution.submit

    def capture(instruction, *, now):
        captured.append(instruction)
        return original(instruction, now=now)

    core.execution.submit = capture
    core.process(at=snapshot.as_of, **tape[snapshot.as_of.isoformat()])
    return core, captured[0], snapshot.as_of


def test_tampering_expiry_and_idempotent_retry(tmp_path):
    core, instruction, at = approved(tmp_path)
    data = json.loads(instruction.payload)
    assert data["mode"] == "PAPER"
    data["quantity"] += 1
    with pytest.raises(ValueError, match="modified"):
        core.execution.submit(replace(instruction, payload=json.dumps(data)), now=at)
    with pytest.raises(ValueError, match="expired"):
        core.execution.submit(instruction, now=at + timedelta(seconds=1))
    core.execution.submit(instruction, now=at)
    assert len(core.simulator.portfolio()["orders"]) == 1


def test_restart_and_unapproved_payload_rejected(tmp_path):
    core, instruction, at = approved(tmp_path)
    other = PaperExecutionBoundary(core.simulator)
    with pytest.raises(ValueError, match="Untrusted"):
        other.submit(instruction, now=at)
    with pytest.raises(TypeError):
        other.submit({"action": "BUY"}, now=at)
    with pytest.raises(ValueError, match="meta"):
        other.authorize(meta={}, risk={}, proposal=None, critic={}, now=at)


@pytest.mark.parametrize("mode", ["LIVE", "live", "", "unknown"])
def test_live_configuration_cannot_enable_execution(monkeypatch, mode):
    monkeypatch.setenv("KIWIT_EXECUTION_MODE", mode)
    simulator = Mock()
    with pytest.raises(ValueError):
        PaperExecutionBoundary(simulator)
    simulator.submit.assert_not_called()
    with pytest.raises(ValueError):
        DisabledGrowwLiveAdapter()


def test_mode_is_rechecked_at_submission(tmp_path, monkeypatch):
    core, instruction, at = approved(tmp_path)
    monkeypatch.setenv("KIWIT_EXECUTION_MODE", "LIVE")
    with pytest.raises(ValueError, match="PAPER"):
        core.execution.submit(instruction, now=at)
