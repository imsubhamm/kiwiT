from __future__ import annotations

import csv
import io
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ARCHIVE = "https://nsearchives.nseindia.com/content/historical/EQUITIES"
INDEX_ARCHIVE = "https://archives.nseindia.com/content/indices"


@dataclass(frozen=True)
class DownloadResult:
    day: date
    equity_found: bool
    index_found: bool


def adjust_splits(frame: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Back-adjust OHLCV to the latest unit basis using dated split ratios.

    ratio is new units per old unit (for example, 10 for a 10-for-1 split).
    """
    adjusted = frame.copy()
    adjusted["date"] = pd.to_datetime(adjusted["date"])
    ordered = actions.copy()
    ordered["ex_date"] = pd.to_datetime(ordered["ex_date"])
    for action in ordered.sort_values("ex_date").itertuples(index=False):
        if action.action_type != "split":
            continue
        mask = adjusted["date"] < action.ex_date
        adjusted.loc[mask, ["open", "high", "low", "close"]] /= float(action.ratio)
        if "volume" in adjusted:
            adjusted.loc[mask, "volume"] *= float(action.ratio)
    return adjusted


def _request(url: str, retries: int = 3) -> bytes | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"nsearchives.nseindia.com", "archives.nseindia.com"}:
        raise ValueError("download URL is not an approved NSE HTTPS origin")
    headers = {"User-Agent": "Mozilla/5.0 TradingKIWI research contact=local"}
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:  # nosec B310
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
        except (TimeoutError, urllib.error.URLError):
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def _extract_equity(day: date, payload: bytes) -> dict | None:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            return None
        text = archive.read(names[0]).decode("utf-8-sig", errors="replace")
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("SYMBOL", "").strip() == "NIFTYBEES" and row.get("SERIES", "").strip() == "EQ":
            return {
                "date": day.isoformat(),
                "open": float(row["OPEN"]),
                "high": float(row["HIGH"]),
                "low": float(row["LOW"]),
                "close": float(row["CLOSE"]),
                "volume": int(float(row["TOTTRDQTY"])),
            }
    return None


def _extract_index(day: date, payload: bytes) -> dict | None:
    text = payload.decode("utf-8-sig", errors="replace")
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("Index Name", "").strip().upper() == "NIFTY 50":
            return {
                "date": day.isoformat(),
                "open": float(row["Open Index Value"].replace(",", "")),
                "high": float(row["High Index Value"].replace(",", "")),
                "low": float(row["Low Index Value"].replace(",", "")),
                "close": float(row["Closing Index Value"].replace(",", "")),
            }
    return None


def _download_day(day: date) -> tuple[dict | None, dict | None, DownloadResult]:
    token = day.strftime("%d%b%Y").upper()
    equity_url = f"{ARCHIVE}/{day:%Y}/{day.strftime('%b').upper()}/cm{token}bhav.csv.zip"
    index_url = f"{INDEX_ARCHIVE}/ind_close_all_{day:%d%m%Y}.csv"
    equity_payload = _request(equity_url)
    index_payload = _request(index_url)
    equity = _extract_equity(day, equity_payload) if equity_payload else None
    index = _extract_index(day, index_payload) if index_payload else None
    return equity, index, DownloadResult(day, equity is not None, index is not None)


def download_dataset(start: date, end: date, output_dir: Path, workers: int = 8) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)

    equities: list[dict] = []
    indices: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_day, day): day for day in days}
        for count, future in enumerate(as_completed(futures), 1):
            equity, index, _ = future.result()
            if equity:
                equities.append(equity)
            if index:
                indices.append(index)
            if count % 250 == 0:
                print(f"checked {count}/{len(days)} weekdays", flush=True)

    if not equities:
        raise RuntimeError("No NIFTYBEES rows were downloaded; refusing to write an empty dataset")
    if not indices:
        raise RuntimeError("No NIFTY 50 index rows were downloaded; refusing to write an empty dataset")

    equity_path = output_dir / "niftybees.csv"
    index_path = output_dir / "nifty50.csv"
    pd.DataFrame(equities).sort_values("date").drop_duplicates("date").to_csv(equity_path, index=False)
    pd.DataFrame(indices).sort_values("date").drop_duplicates("date").to_csv(index_path, index=False)
    return equity_path, index_path
