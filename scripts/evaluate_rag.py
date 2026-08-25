from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.rag import LocalKnowledgeIndex


def main() -> None:
    parser = argparse.ArgumentParser(description="Run kiwiT's frozen retrieval evaluation")
    parser.add_argument("--database", default="data/local/kiwit_knowledge.sqlite3")
    parser.add_argument("--evaluation", default="config/rag_evaluation.json")
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--minimum-recall", type=float, default=0.8)
    args = parser.parse_args()

    cases = json.loads((ROOT / args.evaluation).read_text())
    index = LocalKnowledgeIndex(ROOT / args.database)
    results = []
    try:
        for case in cases:
            hits = index.search(case["question"], limit=args.top_k)
            matching = [hit for hit in hits if case["expected_title_contains"].lower() in hit.title.lower()]
            page_ok = not case.get("require_page") or any(hit.page_start is not None for hit in matching)
            results.append(
                {
                    "question": case["question"],
                    "passed": bool(matching and page_ok),
                    "expected": case["expected_title_contains"],
                    "retrieved": [hit.citation for hit in hits],
                }
            )
    finally:
        index.close()
    recall = sum(result["passed"] for result in results) / len(results) if results else 0.0
    output = {"passed": recall >= args.minimum_recall, "recall_at_k": recall, "top_k": args.top_k, "cases": results}
    print(json.dumps(output, indent=2))
    if not output["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
