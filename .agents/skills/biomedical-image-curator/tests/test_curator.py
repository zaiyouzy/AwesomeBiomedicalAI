from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import curator  # noqa: E402


class CuratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()

    def test_catalog_index_and_three_way_duplicate_check(self) -> None:
        sample = """
**[Example Paper](https://doi.org/10.1038/s41586-026-00000-0)**
<summary><b>ExampleModel</b> — Example Paper</summary>
**[Nature URL Paper](https://www.nature.com/articles/s41467-026-12345-6)**
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "biomedical_images.md"
            path.write_text(sample, encoding="utf-8")
            index = curator.parse_catalog(path)
        self.assertIn("10.1038/s41586-026-00000-0", index["dois"])
        self.assertIn("10.1038/s41467-026-12345-6", index["dois"])
        self.assertEqual(
            ["doi", "title", "model_name"],
            [
                item["field"]
                for item in curator.duplicate_matches(
                    index,
                    "https://doi.org/10.1038/S41586-026-00000-0",
                    "Example Paper",
                    "ExampleModel",
                )
            ],
        )

    def test_deterministic_gate_decisions(self) -> None:
        empty = {"dois": [], "titles": [], "models": []}
        include_candidate = {
            "doi": "10.1038/s41467-026-12345-6",
            "title": "Candidate",
            "journal": "Nature Communications",
            "date": "202608",
        }
        self.assertEqual(
            "needs_review",
            curator.evaluate_metadata(include_candidate, empty, self.rules)["decision"],
        )
        high = dict(include_candidate, title="Deep learning foundation model for retinal imaging")
        self.assertEqual(
            "high",
            curator.evaluate_metadata(high, empty, self.rules)["review_priority"],
        )
        other_page = dict(include_candidate, title="Deep learning triage of pathology images")
        self.assertEqual(
            "other_page",
            curator.evaluate_metadata(other_page, empty, self.rules)["review_priority"],
        )
        old = dict(include_candidate, date="202508")
        self.assertEqual("exclude", curator.evaluate_metadata(old, empty, self.rules)["decision"])
        wrong_venue = dict(include_candidate, journal="IEEE Transactions on Medical Imaging")
        self.assertEqual("exclude", curator.evaluate_metadata(wrong_venue, empty, self.rules)["decision"])

    def valid_record(self) -> dict:
        evidence = [{
            "url": "https://www.nature.com/articles/example",
            "location": "Methods",
            "note": "Supports this field.",
        }]
        return {
            "decision": "include",
            "date": "202608",
            "model_name": "ExampleFM",
            "title": "An example biomedical image model",
            "paper_url": "https://www.nature.com/articles/example",
            "doi": "10.1038/s41467-026-12345-6",
            "venue": "Nature Communications",
            "authors": ["First Author", "Last Author"],
            "model_type": "Biomedical image foundation model",
            "backbone": "Vision Transformer",
            "model_size": {
                "text": "12.3M parameters (reported)",
                "status": "reported",
                "evidence": evidence,
            },
            "training_data": {"text": "10,000 images from 1,000 patients", "evidence": evidence},
            "training_adaptation": {
                "text": "Masked-image pretraining followed by full task fine-tuning",
                "evidence": evidence,
            },
            "downstream_tasks": {
                "text": "Disease classification and lesion segmentation",
                "evidence": evidence,
            },
            "modalities": ["ultrasound"],
            "resources": [{"label": "Code", "url": "https://github.com/example/model"}],
            "performance": [{
                "benchmark": "External cohort",
                "metric": "AUROC",
                "value": "0.91",
                "note": "Held-out hospital",
            }],
            "verification_note": "The parameter count is reported by the article.",
            "supplement_url": "https://www.nature.com/articles/example-supp",
            "notebooklm_url": None,
        }

    def test_record_validation_and_rendering(self) -> None:
        record = self.valid_record()
        self.assertEqual([], curator.validate_record(record, self.rules))
        rendered = curator.render_record(record, self.rules)
        self.assertIn("| 202608 | [ExampleFM](#model-examplefm-202608)", rendered)
        self.assertIn("<details>", rendered)
        self.assertIn("**Reported performance**", rendered)
        self.assertIn("**Verification note:**", rendered)

    def test_validator_rejects_guessable_or_unsupported_record(self) -> None:
        record = self.valid_record()
        record["training_adaptation"] = {
            "text": "supervised",
            "evidence": record["training_data"]["evidence"],
        }
        record["model_size"]["evidence"] = []
        errors = curator.validate_record(record, self.rules)
        self.assertTrue(any("concrete mechanism" in error for error in errors))
        self.assertTrue(any("model_size.evidence" in error for error in errors))

    def test_record_template_is_valid_json(self) -> None:
        template = SKILL_DIR / "assets" / "paper-record.template.json"
        self.assertIsInstance(json.loads(template.read_text(encoding="utf-8")), dict)


if __name__ == "__main__":
    unittest.main()
