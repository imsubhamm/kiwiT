from __future__ import annotations

import csv
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .downloader import NSEArchiveDownloader
from .manifest import ManifestLog
from .models import NormalizedBar, ValidationReport
from .parsers import parse_index_snapshot, parse_legacy_equity, parse_udiff_equity
from .reference import load_corporate_action_dates
from .validation import validate_alignment, validate_bars


class MarketDataPipeline:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.manifest = ManifestLog(self.root / "manifests" / "downloads.jsonl")
        self.downloader = NSEArchiveDownloader(self.root, self.manifest)

    def ingest_range(self, start: date, end: date, symbol: str = "NIFTYBEES", workers: int = 10) -> dict:
        equity: list[NormalizedBar] = []
        index: list[NormalizedBar] = []
        days = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor += timedelta(days=1)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self._ingest_day, day, symbol): day for day in days}
            for checked, future in enumerate(as_completed(futures), 1):
                equity_bar, index_bar = future.result()
                if equity_bar:
                    equity.append(equity_bar)
                if index_bar:
                    index.append(index_bar)
                if checked % 100 == 0:
                    print(f"checked {checked}/{len(days)} weekdays", flush=True)

        actions_path = Path("data/reference/niftybees_corporate_actions.csv")
        actions = load_corporate_action_dates(actions_path) if actions_path.exists() else set()
        reports = [validate_bars(symbol, equity, actions), validate_bars("NIFTY 50", index), validate_alignment(symbol, equity, "NIFTY 50", index)]
        publishable = all(report.publishable for report in reports)
        if publishable:
            self._write_bars(self.root / "normalized" / "nse" / f"{symbol.lower()}.csv", equity)
            self._write_bars(self.root / "normalized" / "nse" / "nifty50.csv", index)
        report_path = self.root / ("published" if publishable else "quarantine") / f"validation_{start}_{end}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"publishable": publishable, "reports": [self._report_dict(report) for report in reports]}, indent=2) + "\n")
        return {"publishable": publishable, "equity_rows": len(equity), "index_rows": len(index), "report": str(report_path)}

    def _ingest_day(self, day: date, symbol: str) -> tuple[NormalizedBar | None, NormalizedBar | None]:
        equity_path = self.downloader.download_equity(day)
        index_path = self.downloader.download_index(day)
        equity_bar = None
        index_bar = None
        if equity_path:
            parser = parse_legacy_equity if day < date(2024, 7, 8) else parse_udiff_equity
            equity_bar = parser(equity_path, symbol)
        if index_path:
            index_bar = parse_index_snapshot(index_path)
        return equity_bar, index_bar

    @staticmethod
    def _write_bars(path: Path, bars: list[NormalizedBar]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = ["trading_date", "symbol", "series", "open", "high", "low", "close", "volume", "source_sha256", "source_format", "isin"]
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for bar in sorted(bars, key=lambda item: item.trading_date):
                value = asdict(bar)
                value["trading_date"] = bar.trading_date.isoformat()
                writer.writerow(value)

    @staticmethod
    def _report_dict(report: ValidationReport) -> dict:
        return {"dataset_name": report.dataset_name, "row_count": report.row_count, "publishable": report.publishable,
                "issues": [{**asdict(issue), "trading_date": issue.trading_date.isoformat() if issue.trading_date else None} for issue in report.issues]}
