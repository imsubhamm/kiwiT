from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import ManifestEntry


class ManifestLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, entry: ManifestEntry) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry.to_json_dict(), sort_keys=True) + "\n")

    def successful_hashes(self) -> set[str]:
        if not self.path.exists():
            return set()
        return {
            record["sha256"]
            for record in (json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip())
            if record["status"] == "downloaded"
        }
