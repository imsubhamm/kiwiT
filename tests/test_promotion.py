# ruff: noqa: F811
from datetime import timedelta

import pytest

pytest.importorskip("xgboost")
from test_trend_model import dataset  # noqa: F401

from kiwit.ml.datasets import _hash
from kiwit.ml.framework import ModelRegistry, TrainingRun
from kiwit.ml.promotion import PromotionWorkflow, timestamp


def setup(dataset, tmp_path):
    registry = ModelRegistry(tmp_path / "models.db")
    champion = registry.train_and_register(TrainingRun("trend", dataset["fingerprint"], {"rounds": 2}), dataset)
    registry.activate("trend", champion)
    workflow = PromotionWorkflow(registry.path)
    start = timestamp(dataset["splits"]["test_end"]) + timedelta(days=1)
    folds = [
        {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "data_fingerprint": "frozen-tape"}
    ]
    job = workflow.plan(TrainingRun("trend", dataset["fingerprint"], {"rounds": 3}), dataset, folds=folds)
    return workflow, champion, job, folds


def evaluator(champion, *, trades=40, bad_binding=False):
    def evaluate(kind, fingerprint, fold):
        report = {
            "version": "unified-replay-v1",
            "model_bindings": {kind: "wrong" if bad_binding else fingerprint},
            "configuration": {"fixed": True},
            "portfolio": {"metadata": {"initial_capital": "1000"}},
            "equity_curve": [{"at": fold["start"], "equity": "1000"}, {"at": fold["end"], "equity": "1010"}],
            "metrics": {
                "trades": trades,
                "expectancy": "1" if fingerprint == champion else "2",
                "maximum_drawdown": ".02",
            },
        }
        report["fingerprint"] = _hash(report)
        return {"data_fingerprint": fold["data_fingerprint"], "report": report}

    return evaluate


def test_training_comparison_manual_activation_and_rollback(dataset, tmp_path):
    workflow, champion, job, folds = setup(dataset, tmp_path)
    at = folds[-1]["end"]
    challenger = workflow.retrain(job)
    assert workflow.retrain(job) == challenger
    with workflow._connect() as db:
        assert workflow._current(db, "trend") == champion
    with pytest.raises(ValueError, match="Evaluation required"):
        workflow.approve_and_promote(job, actor="operator", reason="review", at=at)
    result = workflow.compare(job, evaluator(champion))
    assert result["passed"] is True
    with pytest.raises(ValueError, match="identity"):
        workflow.approve_and_promote(job, actor="", reason="review", at=at)
    action = workflow.approve_and_promote(job, actor="operator", reason="held-out evidence reviewed", at=at)
    with workflow._connect() as db:
        assert workflow._current(db, "trend") == challenger
    workflow.restore(action, actor="operator", reason="paper degradation", at=at)
    with workflow._connect() as db:
        assert workflow._current(db, "trend") == champion
        assert db.execute("SELECT COUNT(*) FROM learning_actions").fetchone()[0] == 2
    with pytest.raises(ValueError, match="already promoted"):
        workflow.approve_and_promote(job, actor="operator", reason="repeat", at=at)


def test_gates_and_direct_activation_blocked(dataset, tmp_path):
    workflow, champion, job, folds = setup(dataset, tmp_path)
    workflow.retrain(job)
    assert workflow.compare(job, evaluator(champion, trades=2))["passed"] is False
    with pytest.raises(ValueError, match="gates"):
        workflow.approve_and_promote(job, actor="operator", reason="profitable", at=folds[-1]["end"])
    with pytest.raises(ValueError, match="approve_and_promote"):
        workflow.activate("trend", champion)


def test_wrong_model_and_training_overlap_rejected(dataset, tmp_path):
    workflow, champion, job, folds = setup(dataset, tmp_path)
    workflow.retrain(job)
    with pytest.raises(ValueError, match="Wrong evaluated"):
        workflow.compare(job, evaluator(champion, bad_binding=True))
    folds[0]["start"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="held-out"):
        workflow.plan(TrainingRun("trend", dataset["fingerprint"]), dataset, folds=folds)


def test_champion_change_blocks_stale_approval(dataset, tmp_path):
    workflow, champion, job, folds = setup(dataset, tmp_path)
    challenger = workflow.retrain(job)
    workflow.compare(job, evaluator(champion))
    # Simulate a separate research operator changing the underlying registry.
    ModelRegistry(workflow.path).activate("trend", challenger)
    with pytest.raises(ValueError, match="Champion changed"):
        workflow.approve_and_promote(job, actor="operator", reason="stale review", at=folds[-1]["end"])
    with workflow._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM learning_actions").fetchone()[0] == 0
