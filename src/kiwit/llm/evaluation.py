"""Frozen paired evaluation without runtime promotion or outcome exposure."""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from kiwit.marketdata.canonical import utc
from kiwit.ml.datasets import _hash, _json

from .context import ContextRequest, evaluate_scenarios
from .critic import CriticRequest, benchmark_critic

VERSION = "llm-corpus-v1"


def freeze_corpus(context, critic, *, source_version, split):
    if not source_version or split not in {"validation", "test", "walk_forward"}:
        raise ValueError("Corpus source version and split required")
    for item in context:
        ContextRequest.model_validate(item["input"])
        if set(item) != {"input", "event_risk", "quant_conflict"}:
            raise ValueError("Context gold labels must remain separate")
        if item["event_risk"] not in {"LOW", "HIGH"} or item["quant_conflict"] not in {"YES", "NO"}:
            raise ValueError("Invalid context gold label")
    for item in critic:
        CriticRequest.model_validate(item["input"])
        if set(item["outcome"]) != {"net_return", "exit_at"}:
            raise ValueError("Invalid outcome schema")
        net = Decimal(str(item["outcome"]["net_return"]))
        if (
            not net.is_finite()
            or net <= -1
            or utc(datetime.fromisoformat(item["outcome"]["exit_at"]))
            <= utc(datetime.fromisoformat(item["input"]["as_of"]))
        ):
            raise ValueError("Invalid or nonfuture outcome")
        if set(item) != {"input", "outcome"}:
            raise ValueError("Outcomes must remain separate")
    if not context or not critic:
        raise ValueError("Both roles need frozen scenarios")
    body = json.loads(
        _json(
            {"version": VERSION, "source_version": source_version, "split": split, "context": context, "critic": critic}
        )
    )
    return {**body, "fingerprint": _hash(body)}


def save_artifact(artifact, directory):
    body = dict(artifact)
    fingerprint = body.pop("fingerprint")
    if _hash(body) != fingerprint:
        raise ValueError("Artifact checksum mismatch")
    path = Path(directory) / f"{fingerprint}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _json(artifact) + "\n"
    try:
        with path.open("x") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_text() != content:
            raise ValueError("Immutable artifact conflict") from None
    return path


async def compare_models(corpus, entrants, *, repeats=2):
    """Entrants map names to (ContextAnalyst, TradeCritic) configured independently."""
    body = dict(corpus)
    fingerprint = body.pop("fingerprint")
    if _hash(body) != fingerprint or body["version"] != VERSION:
        raise ValueError("Corpus checksum/version mismatch")
    freeze_corpus(body["context"], body["critic"], source_version=body["source_version"], split=body["split"])
    if len(entrants) < 2:
        raise ValueError("At least two independently configured entrants required")
    reports = {}
    for name in sorted(entrants):
        analyst, critic = entrants[name]
        context_report = await evaluate_scenarios(analyst, json.loads(_json(body["context"])), repeats=repeats)
        critic_report = await benchmark_critic(critic, json.loads(_json(body["critic"])), repeats=repeats)
        daily = {}
        for row in critic_report["rows"]:
            day = row["at"][:10]
            bucket = daily.setdefault(day, {"known_cost_usd": 0.0, "unknown_cost_attempts": 0, "evaluations": 0})
            for review in row["repeated_reviews"]:
                bucket["evaluations"] += 1
                for attempt in review["gateway"]["attempts"]:
                    if attempt["cost_usd"] is None:
                        bucket["unknown_cost_attempts"] += 1
                    else:
                        bucket["known_cost_usd"] += attempt["cost_usd"]
        count = len(critic_report["rows"]) * repeats
        reports[name] = {
            "context_config": analyst.gateway.config.model_dump(),
            "critic_config": critic.gateway.config.model_dump(),
            "context": context_report,
            "critic": critic_report,
            "critic_cost_by_utc_day": daily,
            "known_critic_cost_per_evaluation": critic_report["known_cost_usd"] / count if count else None,
        }
    report = {
        "version": "paired-llm-eval-v1",
        "corpus_fingerprint": fingerprint,
        "split": body["split"],
        "repeats": repeats,
        "entrants": reports,
        "promotion": "NOT_AUTHORIZED",
        "promotion_requirements": [
            "UNTOUCHED_OUT_OF_SAMPLE_OR_WALK_FORWARD",
            "STATISTICALLY_CREDIBLE_NET_IMPROVEMENT",
            "PAPER_SHADOW_VALIDATION",
            "HUMAN_APPROVAL",
        ],
        "hallucination_review": "Semantic claims require independent human adjudication; schema failures are not a hallucination rate",
    }
    return {**report, "fingerprint": _hash(report)}
