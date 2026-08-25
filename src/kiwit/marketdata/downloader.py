from __future__ import annotations

import hashlib
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path

from .manifest import ManifestLog
from .models import ManifestEntry

USER_AGENT = "Mozilla/5.0 kiwiT market-data research"


class NSEArchiveDownloader:
    def __init__(self, root: str | Path, manifest: ManifestLog, retries: int = 3) -> None:
        self.root = Path(root)
        self.manifest = manifest
        self.retries = retries

    @staticmethod
    def equity_uri(day: date) -> tuple[str, str]:
        if day < date(2024, 7, 8):
            token = day.strftime("%d%b%Y").upper()
            filename = f"cm{token}bhav.csv.zip"
            return f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{day:%Y}/{day.strftime('%b').upper()}/{filename}", filename
        filename = f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
        return f"https://nsearchives.nseindia.com/content/cm/{filename}", filename

    @staticmethod
    def index_uri(day: date) -> tuple[str, str]:
        filename = f"ind_close_all_{day:%d%m%Y}.csv"
        return f"https://archives.nseindia.com/content/indices/{filename}", filename

    def _download(self, source_name: str, uri: str, destination: Path, day: date) -> ManifestEntry | None:
        parsed = urllib.parse.urlsplit(uri)
        if parsed.scheme != "https" or parsed.hostname not in {"nsearchives.nseindia.com", "archives.nseindia.com"}:
            raise ValueError("market-data download URL is not an approved NSE HTTPS origin")
        if destination.exists() and destination.stat().st_size:
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            return ManifestEntry(source_name, uri, str(destination), day, datetime.now(UTC), digest, destination.stat().st_size, "cached", {})
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".partial")
        for attempt in range(self.retries):
            try:
                request = urllib.request.Request(uri, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
                with urllib.request.urlopen(request, timeout=45) as response, partial.open("wb") as output:  # nosec B310
                    shutil.copyfileobj(response, output)
                if partial.stat().st_size == 0:
                    raise ValueError("empty response")
                partial.replace(destination)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                entry = ManifestEntry(source_name, uri, str(destination), day, datetime.now(UTC), digest, destination.stat().st_size, "downloaded", {})
                self.manifest.append(entry)
                return entry
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    partial.unlink(missing_ok=True)
                    return None
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise
            except (TimeoutError, urllib.error.URLError, ValueError):
                pass
            partial.unlink(missing_ok=True)
            time.sleep(1.5 * (attempt + 1))
        return None

    def download_equity(self, day: date) -> Path | None:
        uri, filename = self.equity_uri(day)
        destination = self.root / "raw" / "nse" / "cm" / f"{day:%Y}" / f"{day:%m}" / filename
        entry = self._download("nse_cm_bhavcopy", uri, destination, day)
        return Path(entry.local_path) if entry else None

    def download_index(self, day: date) -> Path | None:
        uri, filename = self.index_uri(day)
        destination = self.root / "raw" / "nse" / "indices" / f"{day:%Y}" / f"{day:%m}" / filename
        entry = self._download("nse_index_snapshot", uri, destination, day)
        return Path(entry.local_path) if entry else None
