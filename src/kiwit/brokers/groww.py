from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

GROWW_ORIGIN = "https://api.groww.in"
IDENTIFIER = re.compile(r"^[A-Za-z0-9-]{1,80}$")
SYMBOL = re.compile(r"^[A-Z0-9&._-]{1,80}$")


class BrokerApiError(RuntimeError):
    """Sanitized broker failure; credentials and raw authorization data are never included."""


class BrokerExecutionDisabled(PermissionError):
    pass


@dataclass(frozen=True)
class GrowwSettings:
    access_token: str = field(repr=False)
    api_secret: str = field(default="", repr=False)
    timeout_seconds: int = 8
    base_url: str = GROWW_ORIGIN
    allow_order_mutations: bool = False
    token_cache_path: str = "/opt/kiwit/shared/groww-access-token.json"

    @classmethod
    def from_env(cls) -> GrowwSettings:
        token = os.getenv("KIWIT_GROWW_ACCESS_TOKEN", "")
        if len(token) < 20:
            raise ValueError("KIWIT_GROWW_ACCESS_TOKEN is not configured")
        timeout = int(os.getenv("KIWIT_BROKER_TIMEOUT_SECONDS", "8"))
        if not 1 <= timeout <= 30:
            raise ValueError("broker timeout must be between 1 and 30 seconds")
        return cls(
            access_token=token, api_secret=os.getenv("KIWIT_GROWW_API_SECRET", ""), timeout_seconds=timeout,
            token_cache_path=os.getenv("KIWIT_GROWW_TOKEN_CACHE", "/opt/kiwit/shared/groww-access-token.json"),
        )

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.groww.in" or parsed.path not in {"", "/"}:
            raise ValueError("Groww base URL must be the official HTTPS API origin")


Transport = Callable[[urllib.request.Request, int], tuple[int, bytes]]


def _urlopen_transport(request: urllib.request.Request, timeout: int) -> tuple[int, bytes]:
    # The URL origin is fixed and validated by GrowwSettings.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return response.status, response.read()


class GrowwBrokerClient:
    """Minimal Groww REST adapter. Mutations remain disabled until a later live-trading approval phase."""

    def __init__(self, settings: GrowwSettings, transport: Transport = _urlopen_transport) -> None:
        self.settings = settings
        self.transport = transport

    def _authorization_token(self) -> str:
        if not self.settings.api_secret:
            return self.settings.access_token
        cache = Path(self.settings.token_cache_path)
        try:
            saved = json.loads(cache.read_text())
            expires = datetime.fromisoformat(saved["expiry"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires > datetime.now(UTC) + timedelta(minutes=2) and len(saved.get("token", "")) >= 20:
                return saved["token"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        timestamp = str(int(time.time()))
        checksum = hashlib.sha256((self.settings.api_secret + timestamp).encode()).hexdigest()
        request = urllib.request.Request(
            f"{self.settings.base_url}/v1/token/api/access", method="POST",
            data=json.dumps({"key_type": "approval", "checksum": checksum, "timestamp": timestamp}).encode(),
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "Authorization": f"Bearer {self.settings.access_token}"},
        )
        try:
            status_code, body = self.transport(request, self.settings.timeout_seconds)
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise BrokerApiError("Groww session approval is required") from error
            raise BrokerApiError(f"Groww token request rejected with HTTP {error.code}") from error
        except (TimeoutError, urllib.error.URLError) as error:
            raise BrokerApiError("Groww token request unavailable") from error
        if status_code != 200:
            raise BrokerApiError(f"Groww token endpoint returned HTTP {status_code}")
        try:
            decoded = json.loads(body)
            token = decoded["token"]
            expiry = decoded.get("expiry") or (datetime.now(UTC) + timedelta(hours=18)).isoformat()
            if len(token) < 20:
                raise ValueError
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise BrokerApiError("Groww token response is invalid") from error
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"token": token, "expiry": expiry}))
            cache.chmod(0o600)
        except OSError as error:
            raise BrokerApiError("Groww token cache is unavailable") from error
        return token

    def _clear_token_cache(self) -> None:
        if not self.settings.api_secret:
            return
        try:
            Path(self.settings.token_cache_path).unlink(missing_ok=True)
        except OSError as error:
            raise BrokerApiError("Groww token cache is unavailable") from error

    def _send(self, method: str, url: str) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._authorization_token()}",
                "X-API-VERSION": "1.0",
                "User-Agent": "kiwiT/0.1 broker-reconciliation",
            },
        )
        return self.transport(request, self.settings.timeout_seconds)

    def _request(self, method: str, path: str, *, query: dict[str, str] | None = None) -> dict[str, Any]:
        if not path.startswith("/v1/") or ".." in path:
            raise ValueError("invalid Groww API path")
        url = f"{self.settings.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        try:
            status_code, body = self._send(method, url)
        except urllib.error.HTTPError as error:
            # A newly approved session/subscription can invalidate an access token
            # that was generated earlier. Refresh once, only for read-only calls.
            if error.code in (401, 403) and method == "GET" and self.settings.api_secret:
                self._clear_token_cache()
                try:
                    status_code, body = self._send(method, url)
                except urllib.error.HTTPError as retry_error:
                    raise BrokerApiError(f"Groww request rejected with HTTP {retry_error.code}") from retry_error
                except (TimeoutError, urllib.error.URLError) as retry_error:
                    raise BrokerApiError("Groww request unavailable") from retry_error
            else:
                raise BrokerApiError(f"Groww request rejected with HTTP {error.code}") from error
        except (TimeoutError, urllib.error.URLError) as error:
            raise BrokerApiError("Groww request unavailable") from error
        if status_code != 200:
            raise BrokerApiError(f"Groww returned unexpected HTTP {status_code}")
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BrokerApiError("Groww returned an invalid JSON response") from error
        if not isinstance(decoded, dict) or decoded.get("status") != "SUCCESS":
            code = decoded.get("error", {}).get("code", "UNKNOWN") if isinstance(decoded, dict) else "UNKNOWN"
            raise BrokerApiError(f"Groww API failure ({code})")
        payload = decoded.get("payload")
        if not isinstance(payload, dict):
            raise BrokerApiError("Groww response payload is missing")
        return payload

    def profile(self) -> dict[str, Any]:
        return self._request("GET", "/v1/user/detail")

    def holdings(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/v1/holdings/user").get("holdings", []))

    def positions(self, segment: str = "CASH") -> list[dict[str, Any]]:
        segment = self._segment(segment)
        return list(self._request("GET", "/v1/positions/user", query={"segment": segment}).get("positions", []))

    def margin(self) -> dict[str, Any]:
        return self._request("GET", "/v1/margins/detail/user")

    def order_status(self, groww_order_id: str, segment: str = "CASH") -> dict[str, Any]:
        if not IDENTIFIER.fullmatch(groww_order_id):
            raise ValueError("invalid Groww order ID")
        return self._request(
            "GET", f"/v1/order/status/{groww_order_id}", query={"segment": self._segment(segment)}
        )

    def quote(self, trading_symbol: str, segment: str = "CASH", exchange: str = "NSE") -> dict[str, Any]:
        symbol = trading_symbol.upper()
        if not SYMBOL.fullmatch(symbol):
            raise ValueError("invalid trading symbol")
        if exchange not in {"NSE", "BSE"}:
            raise ValueError("unsupported exchange")
        return self._request(
            "GET", "/v1/live-data/quote",
            query={"exchange": exchange, "segment": self._segment(segment), "trading_symbol": symbol},
        )

    def place_order(self, _order: dict[str, Any]) -> dict[str, Any]:
        # Deliberately unreachable in this phase: no production code path sends mutation requests.
        raise BrokerExecutionDisabled("Groww order mutations are disabled; kiwiT remains paper-only")

    def banknifty_candles(self, start: datetime, end: datetime) -> dict[str, Any]:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Asia/Kolkata')
        return self._request('GET', '/v1/historical/candles', query={
            'exchange': 'NSE', 'segment': 'CASH', 'groww_symbol': 'NSE-BANKNIFTY',
            'start_time': start.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S'),
            'end_time': end.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S'), 'candle_interval': '1minute',
        })

    def cancel_order(self, _groww_order_id: str) -> dict[str, Any]:
        raise BrokerExecutionDisabled("Groww order mutations are disabled; kiwiT remains paper-only")

    @staticmethod
    def _segment(segment: str) -> str:
        normalized = segment.upper()
        if normalized not in {"CASH", "FNO"}:
            raise ValueError("unsupported Groww segment")
        return normalized
