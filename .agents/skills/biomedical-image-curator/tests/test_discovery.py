from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

SKILL_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import curator  # noqa: E402
import discovery  # noqa: E402

EMPTY_INDEX = {"dois": [], "titles": [], "models": []}


def load_fixture(name: str) -> dict:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class LedgerTests(unittest.TestCase):
    def test_empty_ledger_validates(self) -> None:
        ledger = discovery.empty_ledger("2026-09-04")
        self.assertEqual([], discovery.validate_ledger(ledger))

    def test_valid_entry_and_validation_errors(self) -> None:
        base = discovery.empty_ledger("2026-09-04")
        entry = {
            "identifier": "10.1038/s41467-026-00000-1",
            "identifier_type": "doi",
            "status": "accepted",
            "reviewed_at": "2026-09-04",
            "reason": "Added to the catalogue.",
        }
        base["entries"] = [entry]
        self.assertEqual([], discovery.validate_ledger(base))

        bad = {
            "identifier": "10.1038/s41467-026-00000-1",
            "identifier_type": "unknown_type",
            "status": "maybe",
            "reviewed_at": "04-09-2026",
        }
        base["entries"] = [entry, entry, bad]
        errors = discovery.validate_ledger(base)
        self.assertTrue(any("identifier_type must be one of" in error for error in errors))
        self.assertTrue(any("status must be one of" in error for error in errors))
        self.assertTrue(any("reviewed_at must be YYYY-MM-DD" in error for error in errors))
        self.assertTrue(any("duplicates identifier" in error for error in errors))

    def test_identifier_normalization(self) -> None:
        self.assertEqual(("10.1038/s41467-026-12345-6", "doi"),
                         discovery.normalize_identifier("https://doi.org/10.1038/S41467-026-12345-6"))
        self.assertEqual(("42557331", "pmid"), discovery.normalize_identifier("42557331"))
        # A Nature article URL is normalised to its DOI so ledger entries match
        # candidates that carry only the DOI.
        self.assertEqual(("10.1038/s41586-026-10884-y", "doi"),
                         discovery.normalize_identifier("https://www.nature.com/articles/s41586-026-10884-y"))

    def test_load_missing_ledger_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = discovery.load_ledger(Path(directory) / "missing.json")
        self.assertEqual([], ledger["entries"])

    def test_suppressed_statuses_map(self) -> None:
        ledger = discovery.empty_ledger("2026-09-04")
        ledger["entries"] = [
            {"identifier": "10.1038/s41467-026-00000-1", "identifier_type": "doi",
             "status": "accepted", "reviewed_at": "2026-09-04"},
            {"identifier": "10.1038/s41467-026-00000-2", "identifier_type": "doi",
             "status": "excluded", "reviewed_at": "2026-09-04", "reason": "Not in scope."},
        ]
        mapping = discovery.ledger_suppressed_statuses(ledger)
        self.assertEqual("accepted", mapping.get("doi:10.1038/s41467-026-00000-1"))
        self.assertEqual("excluded", mapping.get("doi:10.1038/s41467-026-00000-2"))


class WindowQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()

    def test_rolling_window_defaults_and_full_year(self) -> None:
        run_date = dt.date(2026, 9, 4)
        start, end = discovery.rolling_range(self.rules, run_date=run_date)
        self.assertEqual(dt.date(2026, 8, 11), start)  # 21 + 3 days back
        self.assertEqual(run_date, end)
        start_full, _ = discovery.rolling_range(self.rules, run_date=run_date, full_year=True)
        self.assertEqual(dt.date(2026, 1, 1), start_full)

    def test_month_code_parsing(self) -> None:
        self.assertEqual("202607", discovery.month_code("2026-07-10"))
        self.assertEqual("202607", discovery.month_code("2026 Jul 10"))
        self.assertEqual("202607", discovery.month_code("2026/07"))
        self.assertEqual("", discovery.month_code(""))
        self.assertEqual("", discovery.month_code("not a date"))

    def test_pass_queries_are_lexically_distinct(self) -> None:
        start, end = dt.date(2026, 8, 11), dt.date(2026, 9, 4)
        queries = discovery.pubmed_queries_v2(self.rules, start, end)
        pass1, pass2 = queries["pubmed_p1"], queries["pubmed_p2"]
        self.assertIn('"[Title/Abstract]', pass1)
        self.assertIn('"[Title]', pass2)
        self.assertNotIn("[Title/Abstract]", pass2)
        # Both passes share the journal list, date window and type exclusions.
        self.assertIn('"Nature Communications"[Journal]', pass1)
        self.assertIn('"Nature Communications"[Journal]', pass2)
        self.assertIn('Editorial[Publication Type]', pass1)
        self.assertIn('Editorial[Publication Type]', pass2)
        self.assertIn('"2026/08/11"[Date - Publication]', pass1)
        self.assertIn('"2026/09/04"[Date - Publication]', pass2)


class RetrospectiveCoverageTests(unittest.TestCase):
    """Regression guards for the 9 eligible retrospective papers and vocab."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()
        cls.fixture = load_fixture("retrospective-eligible.json")

    def test_fixture_discipline_rationale_and_expected(self) -> None:
        self.assertGreaterEqual(len(self.fixture["fixtures"]), 9)
        for entry in self.fixture["fixtures"]:
            self.assertTrue(str(entry.get("rationale") or "").strip(),
                            f"{entry['label']} must record a rationale")
            self.assertIn("both_passes_cover", entry.get("expected", {}))

    def test_recorded_terms_still_in_configured_vocabulary(self) -> None:
        """If a term is deleted from curation-rules.json this test fails."""
        for entry in self.fixture["fixtures"]:
            label = entry["label"]
            for term in entry.get("ai_hits", []):
                self.assertIn(term, self.rules["ai_terms"], f"{label}: {term} lost from ai_terms")
            for term in entry.get("modality_hits", []):
                self.assertIn(term, self.rules["modality_terms"], f"{label}: {term} lost from modality_terms")
            for term in entry.get("excluded_hits", []):
                self.assertIn(term, self.rules["excluded_domains"], f"{label}: {term} lost from excluded_domains")

    def test_coverage_flags_recomputed_from_stored_facts(self) -> None:
        for entry in self.fixture["fixtures"]:
            self.assertEqual(
                bool(entry.get("ai_hits")) and bool(entry.get("modality_hits")),
                entry["pass1"], f"{entry['label']}: pass1 flag inconsistent with recorded hits")
            title = str(entry.get("title") or "")
            self.assertEqual(
                bool(curator.matching_terms(title, self.rules["modality_terms"])),
                entry["pass2_title"], f"{entry['label']}: pass2 flag inconsistent with title")

    def test_union_of_both_passes_covers_all_nine(self) -> None:
        for entry in self.fixture["fixtures"]:
            self.assertTrue(entry["pass1"] or entry["pass2_title"],
                            f"{entry['label']} is covered by neither pass")
        self.assertEqual(9, len(self.fixture["fixtures"]))

    def test_journal_names_canonicalise_to_catalogue_display(self) -> None:
        for entry in self.fixture["fixtures"]:
            canonical = discovery.canonical_journal(entry["journal"], self.rules)
            self.assertEqual(entry["expected_journal"], canonical,
                             f"{entry['label']}: journal not canonicalised")


class FalsePositiveTests(unittest.TestCase):
    """Known false positives must keep their gate classifications."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()
        cls.fixture = load_fixture("false-positives.json")

    def test_fixture_discipline_rationale(self) -> None:
        for entry in self.fixture["fixtures"]:
            self.assertTrue(str(entry.get("rationale") or "").strip(),
                            f"{entry['label']} must record a rationale")

    def test_expected_classifications_hold(self) -> None:
        for entry in self.fixture["fixtures"]:
            metadata = {
                "doi": str(entry.get("doi") or ""),
                "title": entry["title"],
                "abstract": entry.get("abstract") or "",
                "journal": discovery.canonical_journal(entry["journal"], self.rules),
                "publication_date": entry["publication_date"],
            }
            signals = discovery.scope_signals(metadata, self.rules)
            evaluation = curator.evaluate_metadata(metadata, EMPTY_INDEX, self.rules)
            self.assertEqual(entry["expected"]["decision"], evaluation["decision"],
                             f"{entry['label']}: decision mismatch")
            self.assertEqual(entry["expected"]["priority"], signals["review_priority"],
                             f"{entry['label']}: priority mismatch")

    def test_no_false_positive_reaches_the_top_queue_by_signals(self) -> None:
        """other_page candidates are advisory only and never queue automatically."""
        for entry in self.fixture["fixtures"]:
            metadata = {"title": entry["title"], "abstract": entry.get("abstract") or ""}
            signals = discovery.scope_signals(metadata, self.rules)
            if entry["expected"]["priority"] == "other_page":
                self.assertNotIn(signals["review_priority"], ("high", "medium"),
                                 f"{entry['label']} must not be high/medium")


class FirstOnlineDateTests(unittest.TestCase):
    """D5 evidence hierarchy: publisher page -> Crossref -> PubMed epub -> unresolved."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()

    @staticmethod
    def page_text(online_date: str | None):
        def loader(url: str) -> str:
            if online_date is None:
                raise curator.CuratorError("publisher page unavailable")
            return (f'<html><head><meta name="citation_online_date" content="{online_date}">'
                    f'<meta name="citation_doi" content="10.1038/s41467-026-11111-1">'
                    f"</head></html>")
        return loader

    @staticmethod
    def crossref_json(online_parts: list[int] | None):
        def loader(url: str) -> dict:
            if online_parts is None:
                raise curator.CuratorError("HTTP Error 404 for Crossref")
            return {"message": {"published-online": {"date-parts": [online_parts]},
                                "published-print": {"date-parts": [[2026, 5, 1]]}}}
        return loader

    def metadata(self, doi: str = "10.1038/s41467-026-11111-1", epubdate: str = "") -> dict:
        return {"doi": doi, "title": "A foundation model for retinal imaging", "epubdate": epubdate}

    def test_hierarchy_prefers_nature_article_page(self) -> None:
        verify = discovery.verify_first_online(
            self.metadata(), fetch_json=self.crossref_json([2026, 3, 14]),
            fetch_text=self.page_text("2026-03-14"))
        self.assertEqual("202603", verify["first_online"])
        self.assertEqual("verified", verify["date_status"])
        self.assertEqual("nature article page", verify["provenance"][0]["source"])

    def test_crossref_used_when_publisher_unavailable(self) -> None:
        verify = discovery.verify_first_online(
            self.metadata(), fetch_json=self.crossref_json([2026, 4, 2]),
            fetch_text=self.page_text(None))
        self.assertEqual("202604", verify["first_online"])
        self.assertEqual("verified", verify["date_status"])
        self.assertEqual("Crossref published-online", verify["provenance"][0]["source"])

    def test_conflict_is_flagged_not_silently_resolved(self) -> None:
        verify = discovery.verify_first_online(
            self.metadata(), fetch_json=self.crossref_json([2026, 4, 2]),
            fetch_text=self.page_text("2026-03-14"))
        self.assertEqual("conflict", verify["date_status"])
        self.assertEqual("202603", verify["first_online"])  # earliest kept, flagged
        self.assertGreaterEqual(len(verify["provenance"]), 2)

    def test_pubmed_electronic_date_is_the_third_source(self) -> None:
        metadata = self.metadata(epubdate="2026-02-01")
        verify = discovery.verify_first_online(
            metadata, fetch_json=self.crossref_json(None), fetch_text=self.page_text(None))
        self.assertEqual("202602", verify["first_online"])
        self.assertEqual("verified", verify["date_status"])
        self.assertEqual("PubMed electronic", verify["provenance"][0]["source"])

    def test_unresolved_when_all_sources_missing(self) -> None:
        verify = discovery.verify_first_online(
            self.metadata(), fetch_json=self.crossref_json(None), fetch_text=self.page_text(None))
        self.assertEqual("unresolved", verify["date_status"])
        self.assertEqual("", verify["first_online"])

    def test_first_online_gate_rules(self) -> None:
        window_start = dt.date(2026, 8, 11)
        decision, status, _ = discovery.first_online_gate(
            "202511", start=window_start, full_year=False, target_year=2026)
        self.assertEqual(("exclude", "before_target"), (decision, status))
        # A verified date inside the rolling window is kept for review.
        decision, status, _ = discovery.first_online_gate(
            "202608", start=dt.date(2026, 8, 1), full_year=False, target_year=2026)
        self.assertEqual(("", ""), (decision, status))
        decision, status, _ = discovery.first_online_gate(
            "202507", start=window_start, full_year=True, target_year=2026)
        self.assertEqual(("exclude", "before_target"), (decision, status))
        # Verified date inside the whole year but before the rolling window start.
        decision, status, _ = discovery.first_online_gate(
            "202603", start=window_start, full_year=False, target_year=2026)
        self.assertEqual(("exclude", "outside_window"), (decision, status))

    def test_real_trap_fixtures_are_excluded(self) -> None:
        fixture = load_fixture("date-traps.json")
        for entry in fixture["fixtures"]:
            self.assertTrue(str(entry.get("rationale") or "").strip(),
                            f"{entry['label']} must record a rationale")
            year, month, day = (int(part) for part in entry["crossref_online"].split("-"))
            online_parts = [year, month, day]

            def fetch_json(url: str) -> dict:
                if "crossref.org/works/" not in url:
                    raise curator.CuratorError("unexpected url")
                return {"message": {"published-online": {"date-parts": [online_parts]},
                                    "published-print": {"date-parts": [[2026, 2, 1]]}}}

            def fetch_text(url: str) -> str:
                if entry.get("publisher_online"):
                    return (f'<html><head><meta name="citation_online_date" '
                            f'content="{entry["publisher_online"]}"></head></html>')
                raise curator.CuratorError("publisher page unavailable")

            metadata = {
                "doi": entry["doi"],
                "title": entry["title"],
                "journal": entry["journal"],
                "publication_date": entry["publication_date"],
            }
            verify = discovery.verify_first_online(metadata, fetch_json=fetch_json,
                                                   fetch_text=fetch_text)
            self.assertEqual(entry["expected"]["first_online"], verify["first_online"],
                             f"{entry['label']}: first_online mismatch")
            self.assertEqual(entry["expected"]["date_status"], verify["date_status"],
                             f"{entry['label']}: date_status mismatch")
            decision, status, _ = discovery.first_online_gate(
                verify["first_online"], start=dt.date(2026, 8, 11),
                full_year=False, target_year=2026)
            self.assertEqual(entry["expected"]["gate_decision"], decision,
                             f"{entry['label']}: gate decision mismatch")
            self.assertEqual(entry["expected"]["gate_status"], status,
                             f"{entry['label']}: gate status mismatch")


class OfflineEndToEndTests(unittest.TestCase):
    """Deterministic orchestrator run with fully injected fetchers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()

    @staticmethod
    def esummary_payload(records: dict[str, dict]) -> dict:
        uids = sorted(records)
        result: dict = {"uids": uids}
        for pmid in uids:
            record = records[pmid]
            article_ids: list[dict] = []
            if record.get("doi"):
                article_ids.append({"idtype": "doi", "value": record["doi"]})
            result[pmid] = {
                "title": record["title"],
                "fulljournalname": record["journal"],
                "pubdate": record.get("pubdate", "2026 Aug"),
                "articleids": article_ids,
            }
        return {"result": result}

    @staticmethod
    def efetch_xml(pmids: list[str], records: dict[str, dict]) -> str:
        articles: list[str] = []
        for pmid in pmids:
            record = records.get(pmid, {})
            date_block = ""
            if record.get("epub"):
                year, month, day = (str(part) for part in record["epub"])
                date_block = (f"<ArticleDate DateType=\"Electronic\"><Year>{year}</Year>"
                              f"<Month>{month}</Month><Day>{day}</Day></ArticleDate>")
            abstract = record.get("abstract") or ""
            abstract_block = f"<Abstract><AbstractText>{escape(abstract)}</AbstractText></Abstract>" if abstract else ""
            articles.append(
                f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>"
                f"<ArticleTitle>{escape(record.get('title', ''))}</ArticleTitle>"
                f"{abstract_block}{date_block}</Article></MedlineCitation></PubmedArticle>")
        return f"<PubmedArticleSet>{''.join(articles)}</PubmedArticleSet>"

    def candidate_doi(self, suffix: str) -> str:
        return f"10.1038/s41467-026-7{suffix}"

    def standard_universe(self) -> tuple[dict[str, dict], dict[str, dict]]:
        universe: dict[str, dict] = {}
        by_doi: dict[str, dict] = {}

        def add(pmid: str, *, doi: str, title: str, journal: str, abstract: str = "",
                passes: tuple[str, ...] = ("p1",), online=None, epub=None,
                pubdate: str = "2026 Aug 20", page_online: str | None = None) -> None:
            record = {"doi": doi, "title": title, "journal": journal, "abstract": abstract,
                      "passes": passes, "online": online, "epub": epub,
                      "pubdate": pubdate, "page_online": page_online}
            universe[pmid] = record
            by_doi[doi] = record

        add("101", doi=self.candidate_doi("0101"), title="Deep learning foundation model for retinal imaging",
            journal="Nature Communications", passes=("p1",), online=(2026, 8, 20), pubdate="2026 Aug 20")
        add("102", doi=self.candidate_doi("0102"),
            title="Artificial intelligence foundation model for ultrasound screening",
            journal="Nature Communications", passes=("p1", "p2"),
            online=(2026, 8, 22), pubdate="2026 Aug 22")
        add("103", doi=self.candidate_doi("0103"),
            title="A vision-language model for pathology image segmentation",
            journal="Nature Communications", passes=("p2",), online=(2026, 8, 24), pubdate="2026 Aug 24")
        add("104", doi=self.candidate_doi("0104"),
            title="Artificial intelligence for ultrasound screening of carotid stenosis",
            journal="Nature Communications", passes=("p1",), online=(2026, 8, 26), pubdate="2026 Aug 26")
        add("105", doi=self.candidate_doi("0105"), title="Retinal imaging atlas for screening cohorts",
            journal="Nature Communications", abstract="A deep learning model trained on the atlas.",
            passes=("p2",), online=(2026, 8, 28), pubdate="2026 Aug 28")
        add("106", doi=self.candidate_doi("0106"),
            title="Foundation model for retinal ultrasound imaging",
            journal="Nature Communications", passes=("p1",), online=(2025, 11, 3),
            pubdate="2026 Feb 12")
        return universe, by_doi

    def run_with(self, universe: dict[str, dict], by_doi: dict[str, dict],
                 ledger_entries: list[dict], queue_limit: int | None = None,
                 date_verify: bool = True) -> dict:
        # Pass membership is encoded on each record.
        pass_members = {"p1": [], "p2": []}
        for pmid, record in universe.items():
            for pass_name in record.get("passes", ()):
                pass_members[pass_name].append(pmid)

        def fetch_json(url: str) -> dict:
            host = urlparse(url).netloc
            path = urlparse(url).path
            if "eutils.ncbi.nlm.nih.gov" in host and "esearch" in path:
                term = parse_qs(urlparse(url).query).get("term", [""])[0]
                if "[Title/Abstract]" in term:
                    ids = pass_members["p1"]
                elif "[Title]" in term:
                    ids = pass_members["p2"]
                else:
                    ids = []
                return {"esearchresult": {"idlist": ids}}
            if "eutils.ncbi.nlm.nih.gov" in host and "esummary" in path:
                ids = parse_qs(urlparse(url).query).get("id", [""])[0].split(",")
                payload = self.esummary_payload({pmid: universe[pmid] for pmid in ids if pmid in universe})
                return payload
            if "api.crossref.org" in host and "/works/" in path:
                doi = path.split("/works/")[1]
                candidate = by_doi.get(doi)
                if not candidate or not candidate.get("online"):
                    raise curator.CuratorError("Crossref 404")
                return {"message": {"published-online": {"date-parts": [list(candidate["online"])]}}}
            raise curator.CuratorError(f"unexpected json url {url}")

        def fetch_text(url: str) -> str:
            host = urlparse(url).netloc
            path = urlparse(url).path
            if "eutils.ncbi.nlm.nih.gov" in host and "efetch" in path:
                ids = parse_qs(urlparse(url).query).get("id", [""])[0].split(",")
                return self.efetch_xml(ids, universe)
            if "nature.com" in host and "/articles/" in path:
                doi_suffix = path.split("/articles/")[1]
                candidate = by_doi.get(f"10.1038/{doi_suffix}")
                if candidate and candidate.get("page_online"):
                    return (f'<html><head><meta name="citation_online_date" '
                            f'content="{candidate["page_online"]}"></head></html>')
                raise curator.CuratorError("nature page unavailable")
            raise curator.CuratorError(f"unexpected text url {url}")

        catalog_index = {"dois": [self.candidate_doi("0102")], "titles": [], "models": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "reviewed-papers.json"
            ledger = discovery.empty_ledger("2026-09-04")
            ledger["entries"] = ledger_entries
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            payload = discovery.run_discovery_v2(
                repo_root=root, rules=self.rules, source="pubmed",
                queue_limit=queue_limit, ledger_path=ledger_path,
                prefix=str(root / "weekly"), run_date=dt.date(2026, 9, 4),
                catalog_index=catalog_index, fetch_json=fetch_json,
                fetch_text=fetch_text, date_verify=date_verify)
            for key in ("queue_json", "queue_md", "audit_json", "summary_md"):
                self.assertTrue(Path(payload["files"][key]).exists(), key)
            audit = json.loads(Path(payload["files"]["audit_json"]).read_text(encoding="utf-8"))
            payload["_audit_results"] = audit["results"]
            return payload

    def test_standard_universe_classification(self) -> None:
        universe, by_doi = self.standard_universe()
        suppressed = [{
            "identifier": self.candidate_doi("0104"), "identifier_type": "doi",
            "status": "accepted", "reviewed_at": "2026-09-01", "reason": "In catalogue.",
        }]
        payload = self.run_with(universe, by_doi, suppressed)
        counts = payload["counts"]
        self.assertEqual(6, counts.get("total"))
        self.assertEqual(1, counts.get("duplicate"))
        self.assertEqual(1, counts.get("exclude"))  # the 2025-online trap
        queue = payload["queue"]
        queue_dois = {entry["doi"] for entry in queue}
        self.assertEqual({self.candidate_doi("0101"), self.candidate_doi("0105")}, queue_dois)
        self.assertEqual(1, payload["ledger_suppressed"])
        self.assertEqual(6, len(payload["_audit_results"]))
        trap = next(result for result in payload["results"]
                    if result["candidate_id"] == "pmid-106")
        self.assertEqual("exclude", trap["decision"])
        self.assertEqual("before_target", trap["date_status"])
        other_page = next(result for result in payload["results"]
                          if result["candidate_id"] == "pmid-103")
        self.assertEqual("other_page", other_page["review_priority"])
        self.assertNotIn("pmid-103", [entry["candidate_id"] for entry in queue])

    def test_queue_cap_default_and_override(self) -> None:
        universe: dict[str, dict] = {}
        by_doi: dict[str, dict] = {}
        for index in range(1, 13):
            pmid = f"2{index:02d}"
            doi = self.candidate_doi(f"02{index:02d}")
            universe[pmid] = {
                "doi": doi, "title": f"Deep learning foundation model for ultrasound series {index}",
                "journal": "Nature Communications", "abstract": "", "passes": ("p1",),
                "online": (2026, 8, 15), "pubdate": "2026 Aug 15",
            }
            by_doi[doi] = universe[pmid]
        payload = self.run_with(universe, by_doi, ledger_entries=[])
        self.assertEqual(10, payload["queue_size"])
        self.assertEqual(10, payload["run"]["queue_limit"])
        payload_wide = self.run_with(universe, by_doi, ledger_entries=[], queue_limit=25)
        self.assertEqual(12, payload_wide["queue_size"])
        self.assertEqual(12, len(payload_wide["_audit_results"]))


if __name__ == "__main__":
    unittest.main()
