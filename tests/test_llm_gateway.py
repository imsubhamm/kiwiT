import asyncio
import json

import pytest

from kiwit.llm.gateway import GatewayConfig, LLMGateway, Reply, RetryableError


class Audit:
    def __init__(self):
        self.records = []

    def append(self, kind, data):
        self.records.append(data)


class Provider:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def payload(critic=False):
    data = {
        "snapshot_fingerprint": "snapshot",
        "sources": [{"source_id": "s1", "text": "Supplied event"}],
        "quantitative_summary": "Trend confidence .8",
    }
    if critic:
        data.update(candidate_id="c1", direction="LONG", thesis="trend", candidate_confidence=0.8)
    return data


def reply(critic=False, **extra):
    return Reply(
        json.dumps(
            {
                ("verdict" if critic else "event_risk"): ("APPROVE" if critic else "LOW"),
                "confidence": 0.8,
                "reason_codes": ["SUPPLIED_EVIDENCE"],
                "source_ids": ["s1"],
                **({"warnings": []} if critic else {"market_context": "NEUTRAL", "quant_conflict": "NO"}),
                **extra,
            }
        ),
        "deepseek-v4-pro",
        100,
        50,
    )


def test_roles_usage_and_authority():
    provider, audit = Provider([reply(), reply(True)]), Audit()
    gateway = LLMGateway(
        {"deepseek": provider}, audit, GatewayConfig(input_usd_per_million=1.0, output_usd_per_million=2.0)
    )
    context = asyncio.run(gateway.analyze_context(payload()))
    critic = asyncio.run(gateway.critique_trade(payload(True)))
    assert context["status"] == critic["status"] == "VALID"
    assert not critic["execution_allowed"]
    assert provider.calls[0]["system"] != provider.calls[1]["system"]
    assert critic["attempts"][0]["cost_usd"] == pytest.approx(0.0002)
    assert len(audit.records) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not json",
        '{"risk":"LOW","risk":"HIGH"}',
        reply(quantity=10).content,
        reply(source_ids=["invented"]).content,
    ],
)
def test_malformed_or_invented_output_is_unknown(bad):
    provider, audit = Provider([Reply(bad, "model")]), Audit()
    result = asyncio.run(LLMGateway({"deepseek": provider}, audit).analyze_context(payload()))
    assert result["status"] == "UNKNOWN"
    assert result["output"]["event_risk"] == "UNKNOWN"
    assert len(provider.calls) == 1


def test_input_allowlist_and_serious_candidate_gate():
    provider, audit = Provider([]), Audit()
    gateway = LLMGateway({"deepseek": provider}, audit)
    data = payload(True)
    data["candidate_confidence"] = 0.3
    assert asyncio.run(gateway.critique_trade(data))["status"] == "UNCERTAIN"
    data = payload()
    data["broker_token"] = "not-allowed"
    assert asyncio.run(gateway.analyze_context(data))["status"] == "UNKNOWN"
    assert not provider.calls


def test_retry_and_deadline():
    provider = Provider([RetryableError(), reply()])
    result = asyncio.run(LLMGateway({"deepseek": provider}, Audit()).analyze_context(payload()))
    assert result["status"] == "VALID" and len(result["attempts"]) == 2

    class Slow:
        async def complete(self, **kwargs):
            await asyncio.sleep(10)

    result = asyncio.run(
        LLMGateway({"deepseek": Slow()}, Audit(), GatewayConfig(timeout_seconds=0.01, attempts=1)).analyze_context(
            payload()
        )
    )
    assert result["status"] == "UNKNOWN"
    assert result["latency_ms"] < 1000


def test_benchmark_isolated_and_redacted():
    runtime, benchmark, audit = Provider([]), Provider([reply()]), Audit()
    gateway = LLMGateway({"deepseek": runtime, "benchmark": benchmark}, audit, secrets=("sensitive-literal",))
    data = payload()
    data["sources"][0]["text"] = "sensitive-literal"
    result = asyncio.run(
        gateway.benchmark([{"role": "context", "input": data}], provider="benchmark", model="configured-benchmark-id")
    )
    assert gateway.config.provider == "deepseek"
    assert not runtime.calls
    assert result[0]["scenario_fingerprint"]
    assert "sensitive-literal" not in benchmark.calls[0]["user"]
    assert "sensitive-literal" not in json.dumps(audit.records)


def test_audit_failure_does_not_return_approval():
    class Broken:
        def append(self, *args):
            raise OSError("disk full")

    with pytest.raises(OSError):
        asyncio.run(LLMGateway({"deepseek": Provider([reply(True)])}, Broken()).critique_trade(payload(True)))


def test_deepseek_adapter_request_contract(monkeypatch):
    import httpx

    from kiwit.llm.gateway import DeepSeekAdapter

    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert "tools" not in body
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [{"finish_reason": "stop", "message": {"content": reply().content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    result = asyncio.run(LLMGateway({"deepseek": DeepSeekAdapter("fixture-key")}, Audit()).analyze_context(payload()))
    assert result["status"] == "VALID"
    assert len(calls) == 1
