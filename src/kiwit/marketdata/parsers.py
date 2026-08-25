from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import date, datetime
from pathlib import Path

from .models import NormalizedBar


UDIFF_REQUIRED = {"TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol", "TradDt"}
LEGACY_REQUIRED = {"SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "TOTTRDQTY", "TIMESTAMP"}


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _csv_from_zip(path: str | Path) -> tuple[list[dict[str, str]], set[str]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"zip contains no CSV: {path}")
        text = archive.read(names[0]).decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    return rows, set(reader.fieldnames or [])


def _date(value: str) -> date:
    clean = value.strip()
    for pattern in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d-%b-%Y %H:%M:%S"):
        try:
            return datetime.strptime(clean, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported trading date: {value}")


def parse_udiff_equity(path: str | Path, symbol: str, series: str = "EQ") -> NormalizedBar | None:
    rows, fields = _csv_from_zip(path)
    missing = UDIFF_REQUIRED - fields
    if missing:
        raise ValueError(f"UDiFF schema missing fields: {sorted(missing)}")
    digest = file_sha256(path)
    for row in rows:
        if row["TckrSymb"].strip() == symbol and row["SctySrs"].strip() == series:
            return NormalizedBar(
                trading_date=_date(row["TradDt"]), symbol=symbol, series=series,
                open=float(row["OpnPric"]), high=float(row["HghPric"]), low=float(row["LwPric"]), close=float(row["ClsPric"]),
                volume=int(float(row["TtlTradgVol"])), source_sha256=digest, source_format="nse_cm_udiff_v1",
                isin=(row.get("ISIN") or "").strip() or None,
            )
    return None


def parse_legacy_equity(path: str | Path, symbol: str, series: str = "EQ") -> NormalizedBar | None:
    rows, fields = _csv_from_zip(path)
    missing = LEGACY_REQUIRED - fields
    if missing:
        raise ValueError(f"legacy schema missing fields: {sorted(missing)}")
    digest = file_sha256(path)
    for row in rows:
        if row["SYMBOL"].strip() == symbol and row["SERIES"].strip() == series:
            return NormalizedBar(
                trading_date=_date(row["TIMESTAMP"]), symbol=symbol, series=series,
                open=float(row["OPEN"]), high=float(row["HIGH"]), low=float(row["LOW"]), close=float(row["CLOSE"]),
                volume=int(float(row["TOTTRDQTY"])), source_sha256=digest, source_format="nse_cm_legacy",
            )
    return None


def parse_index_snapshot(path: str | Path, index_name: str = "NIFTY 50") -> NormalizedBar | None:
    digest = file_sha256(path)
    with Path(path).open(encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            if row.get("Index Name", "").strip().upper() == index_name.upper():
                number = lambda field: float(row[field].replace(",", ""))  # noqa: E731
                return NormalizedBar(
                    trading_date=_date(row["Index Date"]), symbol=index_name, series="INDEX",
                    open=number("Open Index Value"), high=number("High Index Value"), low=number("Low Index Value"), close=number("Closing Index Value"),
                    volume=None, source_sha256=digest, source_format="nse_index_daily_snapshot",
                )
    return None

