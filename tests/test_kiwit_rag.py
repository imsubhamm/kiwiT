import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.intelligence import KiwiTIntelligence
from kiwit.rag import LocalKnowledgeIndex, chunk_document

SAMPLE = """
=== PAGE 7 ===
Risk management requires traders to define the maximum loss before entry. Discipline matters.

=== PAGE 8 ===
Position sizing connects the distance to the protective stop with account risk.
"""


class RagTests(unittest.TestCase):
    def test_chunks_preserve_page_provenance_and_stable_ids(self):
        first = chunk_document(SAMPLE, "source", target_words=20, overlap_words=2)
        second = chunk_document(SAMPLE, "source", target_words=20, overlap_words=2)
        self.assertEqual(first, second)
        self.assertEqual(first[0].page_start, 7)
        self.assertEqual(first[-1].page_end, 8)

    def test_ingestion_is_idempotent_and_search_has_citation(self):
        with tempfile.TemporaryDirectory() as temp:
            index = LocalKnowledgeIndex(Path(temp) / "knowledge.db")
            first = index.ingest_text("Risk Book", "book", "/books/risk.pdf", SAMPLE)
            second = index.ingest_text("Risk Book", "book", "/books/risk.pdf", SAMPLE)
            self.assertEqual(first, second)
            self.assertEqual(index.stats()["sources"], 1)
            hits = index.search("protective stop position sizing")
            self.assertTrue(hits)
            self.assertIn("Risk Book", hits[0].citation)
            self.assertIn("p.", hits[0].citation)
            index.close()

    def test_intelligence_is_evidence_bound_and_has_no_execution_api(self):
        with tempfile.TemporaryDirectory() as temp:
            index = LocalKnowledgeIndex(Path(temp) / "knowledge.db")
            index.ingest_text("Risk Book", "book", "/books/risk.pdf", SAMPLE)
            intelligence = KiwiTIntelligence(index)
            context = intelligence.prepare("How should a protective stop affect position sizing?")
            self.assertTrue(context.evidence)
            self.assertIn(context.evidence[0].chunk_id[:12], context.prompt)
            self.assertIn("must not invent", context.prompt)
            self.assertFalse(hasattr(intelligence, "execute"))
            index.close()

    def test_blank_question_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            index = LocalKnowledgeIndex(Path(temp) / "knowledge.db")
            with self.assertRaises(ValueError):
                KiwiTIntelligence(index).prepare("  ")
            index.close()
