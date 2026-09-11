"""Controlled research retraining with frozen gates and atomic approved activation."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from kiwit.marketdata.canonical import utc

from .datasets import _hash, _json
from .framework import ModelRegistry, TrainingRun


def timestamp(value):
    return utc(datetime.fromisoformat(value))


def numeric(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("Nonfinite evaluation metric")
    return value


@dataclass(frozen=True)
class PromotionGates:
    minimum_trades_per_fold: int = 30
    minimum_expectancy_improvement: str = "0"
    maximum_drawdown: str = "0.10"
    maximum_drawdown_regression: str = "0"

    def __post_init__(self):
        if type(self.minimum_trades_per_fold) is not int or self.minimum_trades_per_fold < 1:
            raise ValueError("Positive minimum sample count required")
        if numeric(self.minimum_expectancy_improvement) < 0:
            raise ValueError("Improvement gate cannot be negative")
        if not 0 <= numeric(self.maximum_drawdown) <= 1 or not 0 <= numeric(self.maximum_drawdown_regression) <= 1:
            raise ValueError("Invalid drawdown gates")


class PromotionWorkflow(ModelRegistry):
    """Use this registry instead of unrestricted research activation for governed runs."""

    def __init__(self, path):
        super().__init__(path)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS learning_jobs (
                    id TEXT PRIMARY KEY, plan TEXT NOT NULL, dataset TEXT NOT NULL,
                    challenger TEXT, evaluation TEXT);
                CREATE TABLE IF NOT EXISTS learning_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
                    action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
                    at TEXT NOT NULL, previous TEXT, target TEXT NOT NULL,
                    activation_id INTEGER NOT NULL);
            """)

    def activate(self, kind, fingerprint):
        raise ValueError("Use approve_and_promote with frozen evaluation and manual approval")

    def rollback(self, kind):
        raise ValueError("Use restore with an explicit promotion action and operator identity")

    def _current(self, db, kind):
        row = db.execute(
            "SELECT a.fingerprint FROM active p JOIN activations a ON p.activation_id=a.id WHERE p.kind=?",
            (kind,),
        ).fetchone()
        return row[0] if row else None

    def plan(self, run, dataset, *, folds, gates=None):
        gates = gates or PromotionGates()
        body = dict(dataset)
        if body.pop("fingerprint") != _hash(body) or dataset["fingerprint"] != run.dataset_fingerprint:
            raise ValueError("Dataset checksum/binding mismatch")
        if not folds:
            raise ValueError("Held-out evaluation periods required")
        previous = timestamp(dataset["splits"]["validation_end"])
        for fold in folds:
            if set(fold) != {"start", "end", "data_fingerprint"} or not fold["data_fingerprint"]:
                raise ValueError("Frozen evaluation data binding required")
            if not previous <= timestamp(fold["start"]) < timestamp(fold["end"]):
                raise ValueError("Overlapping or non-held-out folds")
            previous = timestamp(fold["end"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            champion = self._current(db, run.kind)
            if champion is None:
                raise ValueError("Bootstrap a research champion before adopting governed workflow")
            artifact = self._load(db, run.kind, champion).artifact
            if timestamp(artifact["splits"]["validation_end"]) > timestamp(folds[0]["start"]):
                raise ValueError("Champion calibration overlaps evaluation")
            plan = {
                "version": "learning-plan-v1",
                "run": asdict(run),
                "champion": champion,
                "folds": folds,
                "gates": asdict(gates),
            }
            job = _hash(plan)
            db.execute(
                "INSERT OR IGNORE INTO learning_jobs(id,plan,dataset) VALUES (?,?,?)",
                (job, _json(plan), _json(dataset)),
            )
        return job

    def _job(self, db, job):
        row = db.execute("SELECT plan,dataset,challenger,evaluation FROM learning_jobs WHERE id=?", (job,)).fetchone()
        if row is None or _hash(json.loads(row[0])) != job:
            raise ValueError("Unknown or corrupt learning job")
        return json.loads(row[0]), json.loads(row[1]), row[2], json.loads(row[3]) if row[3] else None

    def retrain(self, job):
        with self._connect() as db:
            plan, dataset, challenger, _ = self._job(db, job)
        if challenger:
            return challenger
        challenger = self.train_and_register(TrainingRun(**plan["run"]), dataset)
        with self._connect() as db:
            db.execute("UPDATE learning_jobs SET challenger=? WHERE id=? AND challenger IS NULL", (challenger, job))
        return challenger

    def compare(self, job, evaluator):
        """Evaluator(kind, fingerprint, fold) returns a frozen KIW-30 report plus data binding.

        The adapter must use the requested historical tape and isolated simulator/registry.
        It is trusted execution code, not user-supplied model prose.
        """
        with self._connect() as db:
            plan, _, challenger, existing = self._job(db, job)
            if existing:
                return existing
            if not challenger:
                raise ValueError("Retraining must finish first")
            artifact = self._load(db, plan["run"]["kind"], challenger).artifact
        if timestamp(artifact["splits"]["validation_end"]) > timestamp(plan["folds"][0]["start"]):
            raise ValueError("Challenger calibration overlaps evaluation")
        comparisons, passed = [], challenger != plan["champion"]
        gates = PromotionGates(**plan["gates"])
        for fold in plan["folds"]:
            reports = {}
            for role, fingerprint in (("champion", plan["champion"]), ("challenger", challenger)):
                evidence = evaluator(plan["run"]["kind"], fingerprint, dict(fold))
                report = evidence["report"]
                body = dict(report)
                if body.pop("fingerprint") != _hash(body) or report["version"] != "unified-replay-v1":
                    raise ValueError("Invalid evaluation report checksum/version")
                if evidence["data_fingerprint"] != fold["data_fingerprint"]:
                    raise ValueError("Evaluation data differs from plan")
                if report["model_bindings"].get(plan["run"]["kind"]) != fingerprint:
                    raise ValueError("Wrong evaluated model")
                curve = report["equity_curve"]
                times = [timestamp(p["at"]) for p in curve]
                if (
                    not times
                    or times != sorted(set(times))
                    or times[0] != timestamp(fold["start"])
                    or times[-1] != timestamp(fold["end"])
                ):
                    raise ValueError("Evaluation period incomplete or unordered")
                reports[role] = report
            a, b = reports["champion"], reports["challenger"]
            other = lambda r: {k: v for k, v in r["model_bindings"].items() if k != plan["run"]["kind"]}
            if (
                a["configuration"] != b["configuration"]
                or other(a) != other(b)
                or a["portfolio"]["metadata"] != b["portfolio"]["metadata"]
            ):
                raise ValueError("Comparison changed other models, configuration or simulator assumptions")
            metrics_a, metrics_b = a["metrics"], b["metrics"]
            fold_pass = all(
                type(m["trades"]) is int and m["trades"] >= gates.minimum_trades_per_fold
                for m in (metrics_a, metrics_b)
            )
            for m in (metrics_a, metrics_b):
                if not 0 <= numeric(m["maximum_drawdown"]) <= 1:
                    raise ValueError("Invalid drawdown")
            fold_pass = (
                fold_pass
                and metrics_a["expectancy"] is not None
                and metrics_b["expectancy"] is not None
                and numeric(metrics_b["expectancy"])
                > numeric(metrics_a["expectancy"]) + numeric(gates.minimum_expectancy_improvement)
                and numeric(metrics_b["maximum_drawdown"]) <= numeric(gates.maximum_drawdown)
                and numeric(metrics_b["maximum_drawdown"])
                <= numeric(metrics_a["maximum_drawdown"]) + numeric(gates.maximum_drawdown_regression)
            )
            passed = passed and fold_pass
            comparisons.append({"fold": fold, "passed": fold_pass, "reports": reports})
        result = {
            "job": job,
            "passed": bool(passed),
            "comparisons": comparisons,
            "promotion": "MANUAL_APPROVAL_REQUIRED",
            "significance": "NOT_ESTABLISHED",
        }
        result["fingerprint"] = _hash(result)
        with self._connect() as db:
            db.execute("UPDATE learning_jobs SET evaluation=? WHERE id=? AND evaluation IS NULL", (_json(result), job))
            return self._job(db, job)[3]

    def _activate_record(self, db, *, job, action, actor, reason, at, kind, previous, target):
        if not actor.strip() or not reason.strip():
            raise ValueError("Manual operator identity and rationale required")
        at = timestamp(at).isoformat()
        self._load(db, kind, target)
        cursor = db.execute("INSERT INTO activations(kind,fingerprint) VALUES (?,?)", (kind, target))
        activation = cursor.lastrowid
        db.execute("UPDATE active SET activation_id=? WHERE kind=?", (activation, kind))
        cursor = db.execute(
            "INSERT INTO learning_actions(job,action,actor,reason,at,previous,target,activation_id) VALUES (?,?,?,?,?,?,?,?)",
            (job, action, actor, reason, at, previous, target, activation),
        )
        return cursor.lastrowid

    def approve_and_promote(self, job, *, actor, reason, at):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan, _, challenger, evaluation = self._job(db, job)
            if not evaluation:
                raise ValueError("Evaluation required")
            body = dict(evaluation)
            if body.pop("fingerprint") != _hash(body) or evaluation["job"] != job or evaluation["passed"] is not True:
                raise ValueError("Evaluation gates not passed")
            if timestamp(at) < timestamp(plan["folds"][-1]["end"]):
                raise ValueError("Approval precedes evaluation end")
            kind = plan["run"]["kind"]
            if self._current(db, kind) != plan["champion"]:
                raise ValueError("Champion changed; create a new comparison plan")
            if db.execute("SELECT 1 FROM learning_actions WHERE job=? AND action='PROMOTE'", (job,)).fetchone():
                raise ValueError("Job already promoted")
            return self._activate_record(
                db,
                job=job,
                action="PROMOTE",
                actor=actor,
                reason=reason,
                at=at,
                kind=kind,
                previous=plan["champion"],
                target=challenger,
            )

    def restore(self, promotion_id, *, actor, reason, at):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT job,previous,target,at FROM learning_actions WHERE id=? AND action='PROMOTE'", (promotion_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown promotion")
            job, previous, target, promoted_at = row
            plan, _, _, _ = self._job(db, job)
            kind = plan["run"]["kind"]
            if self._current(db, kind) != target or timestamp(at) < timestamp(promoted_at):
                raise ValueError("Stale rollback or timestamp")
            return self._activate_record(
                db,
                job=job,
                action="ROLLBACK",
                actor=actor,
                reason=reason,
                at=at,
                kind=kind,
                previous=target,
                target=previous,
            )
