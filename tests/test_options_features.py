from datetime import UTC, datetime, timedelta

import pytest

from kiwit.options_market import (
    GROWW_OPTION_CHAIN_SOURCE,
    option_chain_features,
    option_feature_coverage,
    option_fields,
)
from kiwit.playbooks import missing_required_option_fields

NOW = datetime(2026, 9, 25, 4, 56, tzinfo=UTC)
SOURCE = {
    "provider": "Groww",
    "endpoint": "option_chain",
    "status": "available",
    "observed_at": NOW.isoformat(),
    "timestamp_basis": "client_receipt",
}


def source(endpoint="option_chain", observed_at=None):
    return {
        "provider": "Groww",
        "endpoint": endpoint,
        "observed_at": observed_at or NOW.isoformat(),
        "timestamp_basis": "client_receipt",
        "source_reference": GROWW_OPTION_CHAIN_SOURCE,
    }


def candidate(symbol, fields):
    return {
        "symbol": symbol,
        "quote": {
            "market_fields": fields,
            "market_field_sources": {field: source() for field in fields},
        },
    }


def test_option_fields_retains_only_provider_values_in_valid_domains():
    values = option_fields({
        "open_interest": "10", "volume": 0, "iv": "18.75", "delta": "-.42",
        "gamma": ".001", "theta": "-12.4", "vega": "6.2",
    })
    assert values == {
        "open_interest": "10", "volume": "0", "implied_volatility": "18.75",
        "delta": "-0.42", "gamma": "0.001", "theta": "-12.4", "vega": "6.2",
    }


@pytest.mark.parametrize(("field", "value"), [
    ("open_interest", -1), ("open_interest", "1.5"), ("volume", "NaN"),
    ("implied_volatility", 0), ("implied_volatility", 501), ("delta", 1.01),
    ("gamma", -0.1), ("theta", "Infinity"), ("vega", -0.1),
])
def test_option_fields_rejects_malformed_values_without_fabrication(field, value):
    assert field not in option_fields({field: value})


def test_option_chain_normalizes_full_and_partial_contracts_with_provenance():
    payload = {"strikes": {"55000": {
        "CE": {
            "trading_symbol": "BANKNIFTY26SEP55000CE", "open_interest": 100, "volume": 20,
            "greeks": {"iv": 18.5, "delta": .52, "gamma": .001, "theta": -10, "vega": 7.5},
        },
        "PE": {
            "trading_symbol": "BANKNIFTY26SEP55000PE", "open_interest": 90,
            "greeks": {"iv": 19.1, "delta": -1.2, "vega": 7.1},
        },
    }}}
    features = option_chain_features(payload, NOW, NOW + timedelta(seconds=1))
    call = features["BANKNIFTY26SEP55000CE"]
    assert set(call["fields"]) == {
        "open_interest", "volume", "implied_volatility", "delta", "gamma", "theta", "vega",
    }
    assert call["field_sources"]["delta"] == source()
    put = features["BANKNIFTY26SEP55000PE"]
    assert put["fields"] == {"open_interest": "90", "implied_volatility": "19.1", "vega": "7.1"}
    assert "delta" not in put["field_sources"]


@pytest.mark.parametrize(("received_at", "now"), [
    (NOW - timedelta(seconds=31), NOW),
    (NOW + timedelta(seconds=1), NOW),
    (NOW.replace(tzinfo=None), NOW),
    (NOW, NOW.replace(tzinfo=None)),
])
def test_option_chain_rejects_stale_future_or_naive_receipt_times(received_at, now):
    with pytest.raises(ValueError, match="stale or future-dated"):
        option_chain_features({"strikes": {}}, received_at, now)


def test_option_chain_rejects_missing_strike_shape():
    with pytest.raises(TypeError, match="strikes are missing"):
        option_chain_features({}, NOW, NOW)


def test_coverage_reports_full_partial_and_unavailable_with_field_evidence():
    all_fields = {
        "open_interest": "100", "volume": "20", "implied_volatility": "18",
        "delta": ".5", "gamma": ".001", "theta": "-10", "vega": "7",
    }
    coverage = option_feature_coverage([
        candidate("CE", all_fields),
        candidate("PE", {key: value for key, value in all_fields.items() if key != "gamma"}),
    ], SOURCE)
    assert coverage["fields"]["delta"] == {
        "available_contracts": 2, "coverage": "full", "sources": ["Groww:option_chain"],
        "latest_observed_at": NOW.isoformat(),
    }
    assert coverage["fields"]["gamma"]["coverage"] == "partial"
    assert coverage["volatility_risk_rules"] == "unavailable"
    assert coverage["fallback"] == "allow_price_only_rules_block_volatility_dependent_rules"

    unavailable = option_feature_coverage([candidate("CE", {})], {**SOURCE, "status": "unavailable"})
    assert all(item["coverage"] == "unavailable" for item in unavailable["fields"].values())
    assert all(item["sources"] == [] for item in unavailable["fields"].values())


def test_coverage_marks_volatility_rules_available_only_when_all_contracts_are_complete():
    fields = {field: "1" for field in ("implied_volatility", "delta", "gamma", "theta", "vega")}
    coverage = option_feature_coverage([candidate("CE", fields), candidate("PE", fields)], SOURCE)
    assert coverage["volatility_risk_rules"] == "available"
    assert coverage["fallback"] == "not_required"


def test_playbook_fallback_allows_price_only_and_blocks_missing_declared_fields():
    contract = candidate("CE", {"implied_volatility": "18"})
    assert missing_required_option_fields(contract, {"required_option_fields": []}) == []
    assert missing_required_option_fields(
        contract, {"required_option_fields": ["vega", "delta", "implied_volatility"]}
    ) == ["delta", "vega"]
