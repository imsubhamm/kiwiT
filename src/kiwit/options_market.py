"""Read-only Bank Nifty contracts and executable quote validation."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .brokers.groww import BrokerApiError
from .chart_analysis import VERSION, analyse, history_context, parse_minutes
from .intraday import IST, _quote_time

OPTION_FIELDS = ("open_interest", "volume", "implied_volatility", "delta", "gamma", "theta", "vega")
VOLATILITY_FIELDS = ("implied_volatility", "delta", "gamma", "theta", "vega")
GROWW_OPTION_CHAIN_SOURCE = "https://api.groww.in/v1/option-chain/exchange/NSE/underlying/BANKNIFTY"


def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("Missing positive market value")
    return number


def option_fields(payload: dict) -> dict[str, str]:
    """Return only finite, domain-valid provider values; never derive or impute them."""
    aliases = {
        "open_interest": ("open_interest", "oi"),
        "volume": ("volume", "total_traded_quantity"),
        "implied_volatility": ("implied_volatility", "iv"),
        "delta": ("delta",), "gamma": ("gamma",), "theta": ("theta",), "vega": ("vega",),
    }
    fields = {}
    for name, keys in aliases.items():
        value = next((payload.get(key) for key in keys if payload.get(key) is not None), None)
        if value is None:
            continue
        try:
            number = Decimal(str(value))
        except (ArithmeticError, ValueError):
            continue
        valid = number.is_finite()
        if name in {"open_interest", "volume"}:
            valid = valid and number >= 0 and number == number.to_integral_value()
        elif name == "implied_volatility":
            valid = valid and 0 < number <= 500
        elif name == "delta":
            valid = valid and -1 <= number <= 1
        elif name in {"gamma", "vega"}:
            valid = valid and number >= 0
        if valid:
            fields[name] = str(number)
    return fields


def option_chain_features(payload: dict, received_at: datetime, now: datetime) -> dict[str, dict]:
    """Normalize Groww option-chain fields with receipt-time point-in-time provenance."""
    if received_at.tzinfo is None or now.tzinfo is None or not 0 <= (now - received_at).total_seconds() <= 30:
        raise ValueError("Option-chain fields are stale or future-dated")
    strikes = payload.get("strikes")
    if not isinstance(strikes, dict):
        raise TypeError("Option-chain strikes are missing")
    result = {}
    for contracts in strikes.values():
        if not isinstance(contracts, dict):
            continue
        for kind in ("CE", "PE"):
            contract = contracts.get(kind)
            if not isinstance(contract, dict):
                continue
            symbol = contract.get("trading_symbol")
            if not isinstance(symbol, str) or not symbol:
                continue
            greeks = contract.get("greeks") if isinstance(contract.get("greeks"), dict) else {}
            fields = option_fields({
                "open_interest": contract.get("open_interest"),
                "volume": contract.get("volume"),
                "iv": greeks.get("iv"),
                "delta": greeks.get("delta"),
                "gamma": greeks.get("gamma"),
                "theta": greeks.get("theta"),
                "vega": greeks.get("vega"),
            })
            if fields:
                result[symbol] = {
                    "fields": fields,
                    "field_sources": {
                        field: {
                            "provider": "Groww",
                            "endpoint": "option_chain",
                            "source_reference": GROWW_OPTION_CHAIN_SOURCE,
                            "observed_at": received_at.isoformat(),
                            "timestamp_basis": "client_receipt",
                        }
                        for field in fields
                    },
                }
    return result


def option_feature_coverage(candidates: list[dict], source: dict) -> dict:
    contracts = len(candidates)
    counts = {
        field: sum(field in candidate["quote"].get("market_fields", {}) for candidate in candidates)
        for field in OPTION_FIELDS
    }
    fields = {}
    for field, count in counts.items():
        evidence = [
            candidate["quote"].get("market_field_sources", {}).get(field)
            for candidate in candidates
            if field in candidate["quote"].get("market_fields", {})
        ]
        evidence = [item for item in evidence if isinstance(item, dict)]
        observed = sorted(item["observed_at"] for item in evidence if item.get("observed_at"))
        fields[field] = {
            "available_contracts": count,
            "coverage": "unavailable" if count == 0 else "full" if count == contracts else "partial",
            "sources": sorted({
                f"{item.get('provider', 'unknown')}:{item.get('endpoint', 'unknown')}"
                for item in evidence
            }),
            "latest_observed_at": observed[-1] if observed else None,
        }
    volatility_complete = contracts > 0 and all(counts[field] == contracts for field in VOLATILITY_FIELDS)
    return {
        "version": "option-feature-coverage-v2",
        "contracts": contracts,
        "available_contracts_by_field": counts,
        "fields": fields,
        "source": source,
        "premium_history": "retained_contract_tape",
        "volatility_surface": "available" if counts["implied_volatility"] >= 3 else "insufficient_provider_fields",
        "volatility_risk_rules": "available" if volatility_complete else "unavailable",
        "fallback": "allow_price_only_rules_block_volatility_dependent_rules" if not volatility_complete else "not_required",
    }


def contracts_from_csv(text: str, today: date) -> list[dict]:
    contracts = []
    for row in csv.DictReader(io.StringIO(text)):
        if (row["exchange"], row["segment"], row["underlying_symbol"]) != ("NSE", "FNO", "BANKNIFTY"):
            continue
        if row["instrument_type"] not in ("CE", "PE"):
            continue
        expiry = date.fromisoformat(row["expiry_date"])
        if expiry <= today:  # initial pilot deliberately excludes expiry day
            continue
        if row["buy_allowed"] != "1" or row["sell_allowed"] != "1" or row["is_reserved"] != "0":
            continue
        lot, freeze = int(row["lot_size"]), int(row["freeze_quantity"])
        if lot <= 0 or freeze <= lot:
            continue
        contracts.append(
            {
                "symbol": row["trading_symbol"],
                "expiry": str(expiry),
                "kind": row["instrument_type"],
                "strike": str(positive(row["strike_price"])),
                "lot": lot,
                "tick": str(positive(row["tick_size"])),
                "freeze": freeze,
            }
        )
    if not contracts:
        raise ValueError("No eligible non-expiry-day Bank Nifty contracts")
    nearest = min(c["expiry"] for c in contracts)
    return [c for c in contracts if c["expiry"] == nearest]


def executable_quote(payload: dict, now: datetime, *, entry: bool = True) -> dict:
    stamp = _quote_time(payload, now)
    if not 0 <= (now - stamp).total_seconds() <= 90:
        raise ValueError("Option quote stale or future-dated")
    depth = payload.get("depth") if isinstance(payload.get("depth"), dict) else {}
    buys = depth.get("buy") if isinstance(depth.get("buy"), list) else []
    sells = depth.get("sell") if isinstance(depth.get("sell"), list) else []
    best_buy = buys[0] if buys and isinstance(buys[0], dict) else {}
    best_sell = sells[0] if sells and isinstance(sells[0], dict) else {}
    bid = positive(payload.get("bid_price") or best_buy.get("price"))
    bid_size = int(payload.get("bid_quantity") or best_buy.get("quantity") or 0)
    if bid_size <= 0:
        raise ValueError("No bid liquidity")
    quote = {"stamp": stamp.isoformat(), "bid": str(bid), "bid_size": bid_size}
    if entry:
        ask = positive(payload.get("offer_price") or best_sell.get("price"))
        ask_size = int(payload.get("offer_quantity") or best_sell.get("quantity") or 0)
        if ask < bid or (ask - bid) / ask > Decimal(".02") or ask_size <= 0:
            raise ValueError("Crossed, illiquid or wide-spread quote")
        quote.update(ask=str(ask), ask_size=ask_size)
    fields = option_fields(payload)
    quote["market_fields"] = fields
    quote["market_field_sources"] = {
        field: {
            "provider": "Groww", "endpoint": "live_data_quote", "observed_at": stamp.isoformat(),
            "timestamp_basis": "provider_last_trade_time",
        }
        for field in fields
    }
    return quote


class BankNiftyMarket:
    def __init__(self, broker, clock=None):
        self.broker = broker
        self.clock = clock or (lambda: datetime.now(UTC))

    def contracts(self, day):
        cache = Path(os.getenv("KIWIT_OPTION_MASTER_CACHE", "/opt/kiwit/shared/option-master.json"))
        try:
            saved = json.loads(cache.read_text())
            if saved["day"] == str(day):
                return contracts_from_csv(saved["csv"], day)
        except (OSError, KeyError, ValueError, TypeError):
            pass
        # No credentials are sent to the fixed public asset origin.
        with urllib.request.urlopen(  # nosec B310
            "https://growwapi-assets.groww.in/instruments/instrument.csv", timeout=15
        ) as response:
            body = response.read(30_000_001)
        if len(body) > 30_000_000:
            raise ValueError("Instrument master exceeds size limit")
        raw = body.decode("utf-8-sig")
        contracts = contracts_from_csv(raw, day)
        # Retain only eligible rows; keep the persistent cache small and daily-scoped.
        rows = list(csv.DictReader(io.StringIO(raw)))
        symbols = {c["symbol"] for c in contracts}
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(r for r in rows if r["trading_symbol"] in symbols)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=cache.parent, delete=False) as handle:
                json.dump({"day": str(day), "csv": buf.getvalue()}, handle)
                temp = handle.name
            os.replace(temp, cache)
        except OSError:
            pass  # Caching cannot make fresh validated data unusable.
        return contracts

    def quote(self, symbol, now, *, entry=True):
        payload = self.broker.quote(symbol, segment="FNO")
        # Validate against receipt-time clock, not a timestamp captured before network I/O.
        return executable_quote(payload, self.clock(), entry=entry)

    def latest_underlying(self, now):
        bars = parse_minutes(self.broker.banknifty_candles(now - timedelta(minutes=5), now), now)
        if not bars or not 0 <= (now - datetime.fromisoformat(bars[-1]["at"])).total_seconds() <= 120:
            raise ValueError("Underlying recheck candles missing or stale")
        return {"at": bars[-1]["at"], "spot": str(bars[-1]["close"])}

    def snapshot(self, now, cached_context=None, tracked_symbols=()):
        local = now.astimezone(IST)
        start = local.replace(hour=9, minute=15, second=0, microsecond=0)
        current = parse_minutes(self.broker.banknifty_candles(start, now), now)
        context = cached_context
        if (not context or context.get("day") != str(local.date()) or context.get("version") != VERSION
                or len(context.get("daily", [])) < 5 or context.get("partial_sessions")
                or context.get("previous_calendar_week", {}).get("coverage", {}).get("status") != "complete"):
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            context = history_context(self.broker.banknifty_candles(midnight - timedelta(days=14), midnight), now)
        analysis = analyse(current, context, now)
        history = [
            {
                "at": b["at"],
                "spot": str(b["close"]),
                "open": str(b["open"]),
                "high": str(b["high"]),
                "low": str(b["low"]),
            }
            for b in current
            if b["at"][:10] == str(local.date())
        ][-20:]
        stamp = datetime.fromisoformat(history[-1]["at"])
        spot = positive(history[-1]["spot"])
        contracts = self.contracts(now.astimezone(IST).date())
        chain_features = {}
        chain_source = {
            "provider": "Groww", "endpoint": "option_chain", "source_reference": GROWW_OPTION_CHAIN_SOURCE,
            "status": "unavailable", "observed_at": None, "timestamp_basis": "client_receipt",
            "reason_code": "OPTION_CHAIN_ENDPOINT_UNAVAILABLE",
        }
        chain_fetch = getattr(self.broker, "option_chain", None)
        if callable(chain_fetch):
            try:
                payload = chain_fetch("BANKNIFTY", contracts[0]["expiry"], exchange="NSE")
                received_at = self.clock()
                chain_features = option_chain_features(payload, received_at, self.clock())
                chain_source.update(
                    status="available" if chain_features else "invalid",
                    observed_at=received_at.isoformat(),
                    reason_code=None if chain_features else "OPTION_CHAIN_NO_VALID_FIELDS",
                    expiry=contracts[0]["expiry"], matched_contracts=len(chain_features),
                )
            except BrokerApiError:
                chain_source["reason_code"] = "OPTION_CHAIN_PROVIDER_ERROR"
            except OSError:
                chain_source["reason_code"] = "OPTION_CHAIN_TRANSPORT_ERROR"
            except (TypeError, ValueError, ArithmeticError):
                chain_source["reason_code"] = "OPTION_CHAIN_VALIDATION_ERROR"
        strikes = sorted({Decimal(c["strike"]) for c in contracts}, key=lambda strike: abs(strike - spot))[:5]
        candidates = []
        quote_failures = {"validation": 0, "transport": 0, "provider": 0}
        eligible_symbols = {c["symbol"] for c in contracts if Decimal(c["strike"]) in strikes}
        tracked = set(tracked_symbols or ())
        for contract in contracts:
            if contract["symbol"] in eligible_symbols | tracked:
                try:
                    quote = self.quote(contract["symbol"], now)
                    enrichment = chain_features.get(contract["symbol"])
                    if enrichment:
                        quote["market_fields"].update(enrichment["fields"])
                        quote["market_field_sources"].update(enrichment["field_sources"])
                    if min(quote["bid_size"], quote["ask_size"]) >= contract["lot"]:
                        candidates.append(dict(contract, quote=quote,
                                               selection_eligible=contract["symbol"] in eligible_symbols,
                                               tracked=contract["symbol"] in tracked))
                except (ValueError, ArithmeticError):
                    quote_failures["validation"] += 1
                except OSError:
                    quote_failures["transport"] += 1
                except BrokerApiError:
                    quote_failures["provider"] += 1
        coverage = option_feature_coverage(candidates, chain_source)
        coverage["quote_failures"] = quote_failures
        return {
            "at": now.isoformat(),
            "spot": str(spot),
            "spot_at": stamp.isoformat(),
            "candidates": candidates,
            "underlying_history": history,
            "underlying_source": "Groww completed 1-minute index candles",
            "chart_analysis": analysis,
            "chart_cache": context,
            "option_feature_coverage": coverage,
        }


def completed_candles(payload, now):
    """Use candle close time, never receipt time; exclude forming/future candles."""
    samples = {}
    if payload.get("interval_in_minutes") != 1:
        raise ValueError("Expected one-minute Bank Nifty candles")
    for row in payload.get("candles", []):
        opened = _quote_time({"timestamp": row[0]}, now)
        closed = opened + timedelta(minutes=1)
        if closed > now or opened.astimezone(IST).date() != now.astimezone(IST).date():
            continue
        op, high, low, close = map(positive, row[1:5])
        if not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError("Invalid underlying candle OHLC")
        samples[closed] = {
            "at": closed.isoformat(),
            "spot": str(close),
            "open": str(op),
            "high": str(high),
            "low": str(low),
        }
    times = sorted(samples)[-20:]
    if len(times) < 5 or not 0 <= (now - times[-1]).total_seconds() <= 120:
        raise ValueError("Bank Nifty completed candles missing or stale")
    if any((b - a).total_seconds() != 60 for a, b in zip(times[-5:], times[-4:])):
        raise ValueError("Bank Nifty completed candles have gaps")
    return [samples[at] for at in times]
