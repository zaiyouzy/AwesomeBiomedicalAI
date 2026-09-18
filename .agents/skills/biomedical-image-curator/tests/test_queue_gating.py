from __future__ import annotations

import sys
from pathlib import Path
import unittest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import curator  # noqa: E402
import discovery  # noqa: E402


def candidate(
    *,
    decision: str = "needs_review",
    priority: str = "high",
    ai: bool = True,
    modality: bool = True,
    doi: str = "10.1038/s41467-026-70001",
    ledger_status: str = "",
    date_status: str = "verified",
) -> dict:
    return {
        "decision": decision,
        "review_priority": priority,
        "ledger_status": ledger_status,
        "date_status": date_status,
        "signals": {
            "ai_title": ["deep learning"] if ai else [],
            "ai_abstract": [],
            "modality_title": ["ultrasound"] if modality else [],
            "modality_abstract": [],
        },
        "metadata": {"doi": doi, "title": "Example", "journal": "Nature Communications"},
    }


class QueueAdmissionTests(unittest.TestCase):
    """The queue is stricter than the audit: both signals required, news excluded."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()
        cls.cfg = discovery.discovery_config(cls.rules)

    def eligible(self, item: dict) -> bool:
        return discovery.queue_eligible(item, self.rules, self.cfg)

    def test_ai_plus_modality_is_queued(self) -> None:
        self.assertTrue(self.eligible(candidate()))

    def test_ai_only_is_not_queued(self) -> None:
        self.assertFalse(self.eligible(candidate(ai=True, modality=False, priority="medium")))

    def test_modality_only_is_not_queued(self) -> None:
        self.assertFalse(self.eligible(candidate(ai=False, modality=True, priority="medium")))

    def test_nature_news_prefix_is_never_queued(self) -> None:
        news = candidate(doi="10.1038/d41586-026-02567-5")
        self.assertFalse(self.eligible(news))

    def test_ledger_decided_and_gates_are_not_queued(self) -> None:
        self.assertFalse(self.eligible(candidate(ledger_status="accepted")))
        self.assertFalse(self.eligible(candidate(decision="duplicate")))
        self.assertFalse(self.eligible(candidate(decision="exclude")))
        self.assertFalse(self.eligible(candidate(priority="low")))
        self.assertFalse(self.eligible(candidate(priority="other_page")))
        self.assertFalse(self.eligible(candidate(date_status="before_target")))

    def test_config_can_relax_the_both_signals_rule(self) -> None:
        relaxed = {"queue_requires_ai_and_modality": False, "queue_excluded_doi_prefixes": []}
        self.assertTrue(discovery.queue_eligible(
            candidate(ai=True, modality=False, priority="medium"), self.rules, relaxed))
        self.assertTrue(discovery.queue_eligible(
            candidate(doi="10.1038/d41586-026-02567-5"), self.rules, relaxed))

    def test_rules_ship_with_strict_queue_admission(self) -> None:
        self.assertTrue(self.cfg.get("queue_requires_ai_and_modality"))
        self.assertIn("10.1038/d41586-", self.cfg.get("queue_excluded_doi_prefixes") or [])


class ImagingVocabularyTests(unittest.TestCase):
    """Terms added after analysing run #8 (missed cryo-ET tool, broad microscopy)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()

    def test_cryo_electron_tomography_is_recognised(self) -> None:
        title = "MemBrain v2: an end-to-end tool for the analysis of membranes in cryo-electron tomography"
        self.assertTrue(discovery.pass_two_title_matches(title, self.rules))
        self.assertTrue(discovery.pass_one_matches(
            title, "We train a deep learning model on cryo-electron tomography volumes.", self.rules))

    def test_brightfield_is_recognised(self) -> None:
        self.assertTrue(discovery.pass_one_matches(
            "Deep learning recognises antibiotic modes of action from brightfield images",
            "We train a convolutional neural network on brightfield images of E. coli.", self.rules))

    def test_added_terms_are_in_the_configured_vocabulary(self) -> None:
        for term in ("cryo-electron tomography", "cryo-EM", "brightfield", "expansion microscopy",
                     "confocal microscopy", "photoacoustic"):
            self.assertIn(term, self.rules["modality_terms"], term)


if __name__ == "__main__":
    unittest.main()
