from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.rag import LocalKnowledgeIndex


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or query kiwiT's local evidence index")
    parser.add_argument("--database", default="data/local/kiwit_knowledge.sqlite3")
    parser.add_argument("--query")
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()
    index = LocalKnowledgeIndex(ROOT / args.database)
    try:
        if args.query:
            hits = index.search(args.query, limit=args.limit)
            print(json.dumps([{"citation": hit.citation, "content": hit.content, "score": hit.score} for hit in hits], indent=2))
            return
        books = sorted((ROOT / "tmp" / "pdfs" / "text").glob("*.txt"))
        notes = sorted((ROOT / "research").glob("*.md")) + sorted((ROOT / "docs").glob("*.md"))
        ingested = {
            "books": index.ingest_files(books, "book"),
            "research_notes": index.ingest_files(notes, "research_note"),
        }
        print(json.dumps({"status": "ok", **index.stats(), "ingested": ingested}, indent=2))
    finally:
        index.close()


if __name__ == "__main__":
    main()
