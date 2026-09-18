"""Deterministic discovery v2 for the Biomedical Images — Other curator skill.

This module implements the improved deterministic stage only (no LLM, no
catalogue edits, no commits).  It deliberately reuses primitives from the
``curator`` module (normalisation, duplicate handling, gates, metadata
helpers, HTTP loads) instead of duplicating them.

Design summary
--------------
- Rolling discovery window (default 21 days + 3 day overlap) instead of a
  whole-year sweep; ``--full-year`` keeps a whole-year mode for benchmarks.
- Two complementary PubMed passes:
    pass 1: (AI terms) AND (modality terms) in title/abstract
    pass 2: modality terms in the title only (no AI requirement)
- PubMed abstracts are fetched once per run (batched ``efetch`` XML).
- First-online date verification follows the D5 hierarchy:
    1. official Nature article page citation metadata
    2. Crossref ``published-online`` metadata
    3. PubMed electronic publication metadata
    unresolved -> marked for manual review; conflicting sources are kept
    visible instead of silently choosing one. Provenance is stored.
- Optional, paginated, rate-limit-tolerant OpenAlex source-scoped sweep.
- A compact human/AI review queue (default cap 10) plus a complete raw
  audit output.
- A human-reviewed, Git-tracked ledger (assets/reviewed-papers.json) of
  already decided DOIs/identifiers; the automation never writes it.

All network-capable functions accept injectable ``fetch_json`` /
``fetch_text`` callables so that tests can run fully offline.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import curator  # same directory; tests add scripts/ to sys.path


UTC = dt.timezone.utc

LEDGER_SCHEMA = "biomedical-images-other.reviewed-papers"
LEDGER_VERSION = 1
LEDGER_STATUSES = {"accepted", "excluded", "other_page"}
IDENTIFIER_TYPES = {"doi", "nature_url", "pmid", "url", "other"}
SUPPRESS_STATUSES = {"accepted", "excluded", "other_page"}  # decided => leave queue

_MONTHS = {name: index for index, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


# ---------------------------------------------------------------------------
# Small date / window helpers
# ---------------------------------------------------------------------------

def today_utc() -> dt.date:
    return dt.datetime.now(UTC).date()


def discovery_config(rules: dict[str, Any]) -> dict[str, Any]:
    return rules.get("discovery") or {}


def rolling_range(
    rules: dict[str, Any],
    run_date: dt.date | None = None,
    full_year: bool = False,
    window_days: int | None = None,
    overlap_days: int | None = None,
) -> tuple[dt.date, dt.date]:
    cfg = discovery_config(rules)
    end = run_date or today_utc()
    if full_year:
        start = dt.date(int(rules.get("target_year", 2026)), 1, 1)
    else:
        window = int(window_days if window_days is not None else cfg.get("window_days", 21))
        overlap = int(overlap_days if overlap_days is not None else cfg.get("overlap_days", 3))
        start = end - dt.timedelta(days=window + overlap)
    return start, end


def pubmed_date_clause(start: dt.date, end: dt.date) -> str:
    def iso(value: dt.date) -> str:
        return f"{value.year}/{value.month:02d}/{value.day:02d}"
    return f'"{iso(start)}"[Date - Publication] : "{iso(end)}"[Date - Publication]'


def month_code(value: Any) -> str:
    """Return a YYYYMM code from common date strings, or '' when unclear."""
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{6}", text):  # already a YYYYMM code
        return text
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match and 1 <= int(match.group(2)) <= 12:
        return match.group(1) + match.group(2)
    match = re.search(r"(\d{4})-(\d{2})$", text)
    if match and 1 <= int(match.group(2)) <= 12:
        return match.group(1) + match.group(2)
    match = re.search(r"(\d{4})\s+([A-Za-z]{3,9})", text)
    if match:
        month = _MONTHS.get(match.group(2).lower()[:3])
        if month:
            return f"{match.group(1)}{month:02d}"
    match = re.search(r"(\d{4})/(\d{2})", text)
    if match and 1 <= int(match.group(2)) <= 12:
        return match.group(1) + match.group(2)
    return ""


# ---------------------------------------------------------------------------
# Vocabulary coverage and query building
# ---------------------------------------------------------------------------

def _hits(text: str, terms: list[str]) -> list[str]:
    return curator.matching_terms(text or "", terms)


def _field_or(terms: list[str], field: str) -> str:
    return " OR ".join(f'"{term}"[{field}]' for term in terms)


def pass_one_matches(title: str, abstract: str, rules: dict[str, Any]) -> bool:
    ai = bool(_hits(title, rules["ai_terms"]) or _hits(abstract, rules["ai_terms"]))
    modality = bool(_hits(title, rules["modality_terms"]) or _hits(abstract, rules["modality_terms"]))
    return ai and modality


def pass_two_title_matches(title: str, rules: dict[str, Any]) -> bool:
    return bool(_hits(title, rules["modality_terms"]))


def pubmed_pass_one_query(rules: dict[str, Any], start: dt.date, end: dt.date) -> str:
    ai = _field_or(rules["ai_terms"], "Title/Abstract")
    modality = _field_or(rules["modality_terms"], "Title/Abstract")
    base = f"({ai}) AND ({modality})"
    return (f"({base}) AND ({curator.journal_clause(rules)}) "
            f"AND ({pubmed_date_clause(start, end)}) NOT ({curator.not_types_clause()})")


def pubmed_pass_two_query(rules: dict[str, Any], start: dt.date, end: dt.date) -> str:
    modality = _field_or(rules["modality_terms"], "Title")
    return (f"({modality}) AND ({curator.journal_clause(rules)}) "
            f"AND ({pubmed_date_clause(start, end)}) NOT ({curator.not_types_clause()})")


def pubmed_queries_v2(rules: dict[str, Any], start: dt.date, end: dt.date) -> dict[str, str]:
    return {
        "pubmed_p1": pubmed_pass_one_query(rules, start, end),
        "pubmed_p2": pubmed_pass_two_query(rules, start, end),
    }


# ---------------------------------------------------------------------------
# PubMed retrieval (esearch + esummary via curator, efetch abstracts here)
# ---------------------------------------------------------------------------

def ncbi_email() -> str:
    return os.environ.get("NCBI_EMAIL", "").strip()


def run_pubmed_pass(
    rules: dict[str, Any],
    start: dt.date,
    end: dt.date,
    pass_name: str,
    query: str,
    retmax: int,
    fetch_json=None,
) -> tuple[list[str], list[dict[str, Any]]]:
    email = ncbi_email()
    ids = curator.pubmed_search_ids(query, retmax, email=email, fetch_json=fetch_json)
    records = curator.pubmed_summary_records(ids, email=email, fetch_json=fetch_json)
    for record in records:
        record["retrieval_pass"] = pass_name
        record.setdefault("discovery_sources", []).append("PubMed")
    return ids, records


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def fetch_abstract_records(
    pmids: list[str],
    fetch_text=None,
    email: str = "",
    batch_size: int = 100,
) -> dict[str, dict[str, Any]]:
    """Fetch efetch XML for PMIDs and return {pmid: {...}} with abstract + dates."""
    loader = fetch_text if fetch_text is not None else curator.request_text
    out: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(pmids), batch_size):
        chunk = pmids[offset:offset + batch_size]
        parameters = {
            "db": "pubmed", "retmode": "xml", "id": ",".join(chunk),
            "tool": "AwesomeBiomedicalAI-curator",
        }
        if email:
            parameters["email"] = email
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode(parameters)
        text = loader(url)
        if not text or "<PubmedArticle" not in text:
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue
        for article in root.findall(".//PubmedArticle"):
            pmid_node = article.find("MedlineCitation/PMID")
            pmid = "".join(pmid_node.itertext()).strip() if pmid_node is not None else ""
            if not pmid:
                continue
            record: dict[str, Any] = {"abstract": "", "electronic_date": "", "journal_date": ""}
            medline = article.find("MedlineCitation")
            article_node = medline.find("Article") if medline is not None else None
            if article_node is None:
                continue
            title = _element_text(article_node.find("ArticleTitle"))
            abstract_parts: list[str] = []
            abstract_node = article_node.find("Abstract")
            if abstract_node is not None:
                for block in abstract_node.findall("AbstractText"):
                    label = block.get("Label") or ""
                    text_block = "".join(block.itertext()).strip()
                    if label:
                        text_block = f"{label}: {text_block}"
                    if text_block:
                        abstract_parts.append(text_block)
            record["abstract"] = "\n".join(abstract_parts).strip()
            # Electronic publication date (ArticleDate DateType="Electronic").
            for article_date in article_node.findall("ArticleDate"):
                if (article_date.get("DateType") or "").lower() == "electronic":
                    year = _element_text(article_date.find("Year"))
                    month = _element_text(article_date.find("Month"))
                    day = _element_text(article_date.find("Day"))
                    if year:
                        record["electronic_date"] = f"{year}-{month or '01'}-{day or '01'}"
            # Journal issue publication date fallback.
            journal_date = article_node.find("Journal/JournalIssue/PubDate")
            if journal_date is not None:
                year = _element_text(journal_date.find("Year"))
                month = _element_text(journal_date.find("Month"))
                if year:
                    record["journal_date"] = f"{year}-{month or '01'}-01"
                else:
                    record["journal_date"] = _element_text(journal_date).strip()
            out[pmid] = record
    return out


def attach_abstracts(records: list[dict[str, Any]], abstracts: dict[str, dict[str, Any]]) -> None:
    for record in records:
        info = abstracts.get(str(record.get("pmid") or ""))
        if not info:
            continue
        record["abstract"] = info.get("abstract") or ""
        if info.get("electronic_date"):
            record["epubdate"] = info["electronic_date"]
        if not record.get("publication_date") and info.get("journal_date"):
            record["publication_date"] = info["journal_date"]


# ---------------------------------------------------------------------------
# OpenAlex sweep (optional, paginated, failure-tolerant)
# ---------------------------------------------------------------------------

def _abstract_from_inverted(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positions: dict[int, str] = {}
    for word, spots in index.items():
        if isinstance(spots, list):
            for spot in spots:
                if isinstance(spot, int):
                    positions[spot] = str(word)
    if not positions:
        return ""
    words = [positions[i] for i in sorted(positions)]
    return " ".join(words)


def openalex_sweep(
    rules: dict[str, Any],
    start: dt.date,
    end: dt.date,
    fetch_json=None,
    email: str = "",
    api_key: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """Source-scoped OpenAlex sweep with cursor pagination and 429 handling."""
    cfg = discovery_config(rules)
    issns: list[str] = []
    for values in (cfg.get("journal_issns") or {}).values():
        issns.extend(values or [])
    if not issns:
        return [], "OpenAlex sweep skipped: no journal ISSNs configured"
    oa_cfg = cfg.get("openalex") or {}
    per_page = int(oa_cfg.get("per_page", 100))
    max_pages = int(oa_cfg.get("max_pages", 3))
    retry_seconds = float(oa_cfg.get("retry_seconds", 10))
    max_retries = int(oa_cfg.get("max_retries", 1))
    loader = fetch_json if fetch_json is not None else curator.request_json

    issn_filter = "locations.source.issn:" + "|".join(issns)
    base_filter = (
        f"{issn_filter},from_publication_date:{start.isoformat()},"
        f"to_publication_date:{end.isoformat()},type:article"
    )
    records: list[dict[str, Any]] = []
    cursor: str | None = "*"
    pages = 0
    last_error = ""
    while cursor and pages < max_pages:
        parameters: dict[str, str] = {
            "filter": base_filter,
            "per-page": str(per_page),
            "cursor": cursor,
            "mailto": email or "awesomebiomedicalai.curator@example.invalid",
            "select": "id,doi,title,publication_date,primary_location,abstract_inverted_index",
        }
        if api_key:
            parameters["api_key"] = api_key
        url = "https://api.openalex.org/works?" + urlencode(parameters)
        data: dict[str, Any] = {}
        success = False
        for attempt in range(max_retries + 1):
            try:
                data = loader(url)
                success = True
                break
            except curator.CuratorError as exc:
                last_error = str(exc)
                if "429" in str(exc) and attempt < max_retries:
                    time.sleep(retry_seconds)  # pragma: no cover - sleep only on real 429
                    continue
                break
        if not success:
            return [], f"OpenAlex unavailable: {last_error}"
        meta = data.get("meta") or {}
        cursor = meta.get("next_cursor")
        pages += 1
        for item in data.get("results", []) if isinstance(data.get("results"), list) else []:
            if not isinstance(item, dict):
                continue
            location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
            source = location.get("source") if isinstance(location.get("source"), dict) else {}
            doi = curator.normalize_doi(str(item.get("doi") or ""))
            publication_date = str(item.get("publication_date") or "")
            title = str(item.get("title") or "")
            abstract = _abstract_from_inverted(item.get("abstract_inverted_index"))
            # Relevance gate for OpenAlex-only records: sweep returns every
            # article of the five journals in the window; keep only records
            # with at least one AI, modality or excluded-domain signal so the
            # queue and audit stay bounded.
            hay = f"{title}\n{abstract}"
            if not (_hits(hay, rules["ai_terms"])
                    or _hits(hay, rules["modality_terms"])
                    or _hits(hay, rules["excluded_domains"])):
                continue
            records.append({
                "openalex_id": str(item.get("id") or ""),
                "doi": doi,
                "doi_url": f"https://doi.org/{doi}" if doi else "",
                "title": title,
                "journal": str(source.get("display_name") or ""),
                "publication_date": publication_date,
                "date": month_code(publication_date),
                "date_source": "OpenAlex publication date; verify first-online month",
                "paper_url": location.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else ""),
                "abstract": abstract,
                "metadata_source": url,
                "retrieval_pass": "openalex",
                "discovery_sources": ["OpenAlex"],
            })
    return records, ""


# ---------------------------------------------------------------------------
# Scope signals (title + abstract aware) and journal canonicalisation
# ---------------------------------------------------------------------------

def scope_signals(metadata: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    title = str(metadata.get("title") or "")
    abstract = str(metadata.get("abstract") or "")
    ai_title = _hits(title, rules["ai_terms"])
    ai_abstract = _hits(abstract, rules["ai_terms"])
    modality_title = _hits(title, rules["modality_terms"])
    modality_abstract = _hits(abstract, rules["modality_terms"])
    excluded_title = _hits(title, rules["excluded_domains"])
    excluded_abstract = _hits(abstract, rules["excluded_domains"])
    excluded_terms = sorted(set(excluded_title) | set(excluded_abstract))
    ai_any = bool(ai_title or ai_abstract)
    modality_any = bool(modality_title or modality_abstract)
    if excluded_terms:
        priority = "other_page"
    elif ai_any and modality_any:
        priority = "high"
    elif ai_any or modality_any:
        priority = "medium"
    else:
        priority = "low"
    return {
        "review_priority": priority,
        "ai_title": ai_title,
        "ai_abstract": ai_abstract,
        "modality_title": modality_title,
        "modality_abstract": modality_abstract,
        "excluded_title": excluded_title,
        "excluded_abstract": excluded_abstract,
        "excluded_terms": excluded_terms,
        "advisory": True,
    }


def canonical_journal(journal: Any, rules: dict[str, Any]) -> str:
    value = str(journal or "").strip()
    if not value:
        return ""
    for candidate in rules["priority_journals"]:
        if value.casefold() == candidate.casefold():
            return candidate
    return value


# ---------------------------------------------------------------------------
# First-online date verification (D5 hierarchy)
# ---------------------------------------------------------------------------

def nature_article_url(doi: str) -> str:
    normalized = curator.normalize_doi(doi)
    if normalized.startswith("10.1038/"):
        return f"https://www.nature.com/articles/{normalized[len('10.1038/'):]}"
    return ""


def verify_first_online(
    metadata: dict[str, Any],
    fetch_json=None,
    fetch_text=None,
) -> dict[str, Any]:
    """Return provenance plus a date_status for the candidate.

    Hierarchy: (1) official Nature article page, (2) Crossref
    published-online (falls back to published/issued within Crossref),
    (3) PubMed electronic date.  Conflicts are flagged, never silently
    resolved.  Returns date_status one of verified | conflict | unresolved.
    """
    provenance: list[dict[str, Any]] = []
    doi = curator.normalize_doi(str(metadata.get("doi") or ""))

    # 1. Official Nature article page.
    article_url = nature_article_url(doi) or str(metadata.get("paper_url") or "")
    if article_url:
        try:
            page = curator.publisher_metadata(article_url, fetch_text=fetch_text)
            code = month_code(page.get("date") or page.get("publication_date"))
            if code:
                provenance.append({
                    "rank": 1, "source": "nature article page", "month": code,
                    "date": page.get("publication_date") or code, "url": article_url,
                })
        except curator.CuratorError:
            pass

    # 2. Crossref published-online (preferred), then published/issued.
    if doi:
        try:
            loader = fetch_json if fetch_json is not None else curator.request_json
            message = loader(f"https://api.crossref.org/works/{doi}").get("message") or {}
            if not isinstance(message, dict):
                message = {}
            rank = 2
            for key in ("published-online", "published", "issued"):
                publication_date, code = curator.date_from_parts(message.get(key))
                if code:
                    provenance.append({
                        "rank": rank, "source": f"Crossref {key}", "month": code,
                        "date": publication_date or code, "url": f"https://doi.org/{doi}",
                    })
                    break
                rank += 1
        except curator.CuratorError:
            pass

    # 3. PubMed electronic publication metadata.
    epub_code = month_code(metadata.get("epubdate"))
    if epub_code:
        provenance.append({
            "rank": 5, "source": "PubMed electronic", "month": epub_code,
            "date": str(metadata.get("epubdate") or epub_code),
            "url": str(metadata.get("paper_url") or ""),
        })

    provenance.sort(key=lambda item: (item["rank"], item["month"]))
    months = sorted({item["month"] for item in provenance if item.get("month")})
    summary: dict[str, Any] = {"provenance": provenance, "first_online": months[0] if months else ""}
    if len(months) > 1:
        summary["date_status"] = "conflict"
    elif months:
        summary["date_status"] = "verified"
    else:
        summary["date_status"] = "unresolved"
    return summary


# ---------------------------------------------------------------------------
# Ledger (human-reviewed state; automation only reads it)
# ---------------------------------------------------------------------------

def empty_ledger(updated: str | None = None) -> dict[str, Any]:
    return {
        "schema": LEDGER_SCHEMA,
        "version": LEDGER_VERSION,
        "updated": updated or today_utc().isoformat(),
        "entries": [],
    }


def normalize_identifier(identifier: str) -> tuple[str, str]:
    text = str(identifier or "").strip()
    doi = curator.extract_doi(text)
    if doi:
        return doi, "doi"
    if "nature.com/articles/" in text or "nature.com/s" in text:
        return text.rstrip("/"), "nature_url"
    if re.fullmatch(r"\d{1,10}", text):
        return text, "pmid"
    if text.startswith("http"):
        return text.rstrip("/"), "url"
    return text, "other"


def validate_ledger(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["ledger must be a JSON object"]
    if data.get("schema") != LEDGER_SCHEMA:
        errors.append(f"ledger schema must be '{LEDGER_SCHEMA}'")
    if data.get("version") != LEDGER_VERSION:
        errors.append(f"ledger version must be {LEDGER_VERSION}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        return errors + ["entries must be a list"]
    seen: set[tuple[str, str]] = set()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            errors.append(f"entries[{position}] must be an object")
            continue
        identifier = str(entry.get("identifier") or "").strip()
        if not identifier:
            errors.append(f"entries[{position}].identifier is required")
        id_type = str(entry.get("identifier_type") or "")
        if id_type not in IDENTIFIER_TYPES:
            errors.append(f"entries[{position}].identifier_type must be one of {sorted(IDENTIFIER_TYPES)}")
        status = str(entry.get("status") or "")
        if status not in LEDGER_STATUSES:
            errors.append(f"entries[{position}].status must be one of {sorted(LEDGER_STATUSES)}")
        reviewed_at = str(entry.get("reviewed_at") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reviewed_at):
            errors.append(f"entries[{position}].reviewed_at must be YYYY-MM-DD")
        reason = entry.get("reason")
        if reason is not None and not isinstance(reason, str):
            errors.append(f"entries[{position}].reason must be a string when present")
        if identifier and id_type:
            key = normalize_identifier(identifier)
            if key in seen:
                errors.append(f"entries[{position}] duplicates identifier {key[0]} ({key[1]})")
            seen.add(key)
    return errors


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise curator.CuratorError(f"Could not read ledger {path}: {exc}") from exc
    errors = validate_ledger(data)
    if errors:
        raise curator.CuratorError("Ledger validation failed:\n- " + "\n- ".join(errors))
    return data


def ledger_suppressed_statuses(ledger: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in ledger.get("entries", []):
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "")
        if status not in SUPPRESS_STATUSES:
            continue
        identifier, id_type = normalize_identifier(str(entry.get("identifier") or ""))
        mapping[f"{id_type}:{identifier.casefold()}"] = status
    return mapping


def candidate_ledger_status(metadata: dict[str, Any], suppressed: dict[str, str]) -> str:
    doi = curator.normalize_doi(str(metadata.get("doi") or ""))
    pmid = str(metadata.get("pmid") or "")
    url = str(metadata.get("paper_url") or "")
    for key in (f"doi:{doi}".casefold() if doi else "",
                f"pmid:{pmid}" if pmid else "",
                f"nature_url:{nature_article_url(doi)}".casefold() if doi and nature_article_url(doi) else "",
                f"url:{url}".casefold() if url.startswith("http") else ""):
        if key and key in suppressed:
            return suppressed[key]
    return ""


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def truncate(text: str, length: int = 2000) -> str:
    text = str(text or "")
    return text if len(text) <= length else text[:length] + " …"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "paper"


def queue_entry(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result["metadata"]
    doi = curator.normalize_doi(str(metadata.get("doi") or ""))
    return {
        "candidate_id": result["candidate_id"],
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "title": metadata.get("title") or "",
        "journal": metadata.get("journal") or "",
        "first_online": result.get("first_online") or "",
        "date_status": result.get("date_status") or "not_verified",
        "date_provenance": result.get("date_provenance") or [],
        "priority": result["review_priority"],
        "retrieval_pass": metadata.get("retrieval_pass") or "",
        "sources": metadata.get("discovery_sources") or [],
        "paper_url": metadata.get("paper_url") or "",
        "abstract": truncate(metadata.get("abstract") or "", 400),
        "reasons": result["reasons"],
        "ledger_status": result.get("ledger_status") or "",
    }


def render_queue_markdown(payload: dict[str, Any], queue: list[dict[str, Any]]) -> str:
    lines = [
        "# Biomedical Images — Other: review queue",
        "",
        f"**Run:** {payload['run']['date']} · window {payload['run']['window']} · "
        f"source(s) {', '.join(payload['run']['sources'])}",
        f"**Queue cap:** {payload['run']['queue_limit']} — only new `high`/`medium` "
        "candidates are listed. This is a suggestion list; no catalogue entry is made "
        "until a maintainer verifies the paper.",
        "",
    ]
    if not queue:
        lines.append("_No new high/medium candidates in this run._")
    for index, entry in enumerate(queue, start=1):
        date = entry.get("first_online") or "unresolved"
        date_code = f"{date[:4]}-{date[4:]}" if len(date) == 6 else date
        lines.extend([
            f"### {index}. {entry['title']}",
            "",
            f"- **Journal:** {entry['journal']} · **First online:** {date_code} "
            f"(`{entry['date_status']}`) · **Priority:** {entry['priority']} · "
            f"**Pass/source:** {entry['retrieval_pass'] or entry['sources']}",
        ])
        if entry.get("doi_url"):
            lines.append(f"- **DOI:** [{entry['doi']}]({entry['doi_url']})")
        if entry.get("date_provenance"):
            provenance = "; ".join(f"{item['source']} {item['month']}" for item in entry["date_provenance"][:3])
            lines.append(f"- **Date provenance:** {provenance}")
        for reason in entry["reasons"][:5]:
            lines.append(f"- {reason}")
        if entry.get("abstract"):
            snippet = entry["abstract"].replace("\n", " ")
            lines.append(f"- **Abstract snippet:** {snippet[:300]}")
        lines.append("")
    lines.append("---")
    lines.append("Next: review each entry's article, supplement, code, weights and "
                 "official data; then either approve it (add to the catalogue and record "
                 "the DOI in `reviewed-papers.json` as `accepted`) or record it as "
                 "`excluded` / `other_page` with a reason.")
    return "\n".join(lines).rstrip() + "\n"


def render_summary_markdown(payload: dict[str, Any], queue: list[dict[str, Any]]) -> str:
    counts = payload["counts"]
    errors = payload.get("source_errors") or {}
    lines = [
        "# Biomedical Images — Other discovery v2 summary",
        "",
        f"**Run date:** {payload['run']['date']}  \n"
        f"**Window:** {payload['run']['window']}  \n"
        f"**Sources:** {', '.join(payload['run']['sources'])}  \n"
        f"**Queue limit:** {payload['run']['queue_limit']}",
        "",
        "## Counts",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Candidates retrieved | {counts.get('total', 0)} |",
        f"| needs_review | {counts.get('needs_review', 0)} |",
        f"| duplicates (already in catalogue) | {counts.get('duplicate', 0)} |",
        f"| excluded | {counts.get('exclude', 0)} |",
        f"| high-priority candidates (AI + modality) | {payload.get('priority_counts', {}).get('high', 0)} |",
        f"| not queued (only one side matched) | "
        f"{payload.get('signal_mix', {}).get('ai_only', 0) + payload.get('signal_mix', {}).get('modality_only', 0)} |",
        f"| suppressed by reviewed-papers ledger | {payload.get('ledger_suppressed', 0)} |",
        f"| Review queue size | {payload.get('queue_size', 0)} |",
        "",
        "Queue admission: "
        + ("an AI term **and** an imaging-modality term are both required"
           if payload.get("queue_admission", {}).get("require_ai_and_modality")
           else "high/medium priority is sufficient")
        + ("; Nature news/commentary DOIs are never queued"
           if payload.get("queue_admission", {}).get("excluded_doi_prefixes") else "")
        + ". Everything else stays visible in the raw audit.",
        "",
        "## Queue",
        "",
    ]
    if queue:
        for entry in queue:
            date = entry.get("first_online") or "unresolved"
            date_code = f"{date[:4]}-{date[4:]}" if len(date) == 6 else date
            lines.append(
                f"- **{entry['title']}** — {entry['journal']} · {date_code} "
                f"(`{entry['date_status']}`) · {entry['priority']} · "
                f"{entry.get('doi_url') or entry.get('paper_url') or ''}"
            )
    else:
        lines.append("- _No new high/medium candidates._")
    if errors:
        lines.extend(["", "## Source status", ""])
        for source, message in errors.items():
            lines.append(f"- **{source}:** {message}")
    lines.extend([
        "",
        "## Files",
        "",
        "- Review queue JSON: `" + payload["files"]["queue_json"] + "`",
        "- Review queue Markdown: `" + payload["files"]["queue_md"] + "`",
        "- Raw audit JSON: `" + payload["files"]["audit_json"] + "`",
        "- This summary: `" + payload["files"]["summary_md"] + "`",
        "",
        "Download the **review-queue** artifact, review the papers against the skill "
        "policy, then update `reviewed-papers.json` (accepted/excluded/other_page) "
        "when you act on the queue.",
    ])
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def queue_eligible(
    result: dict[str, Any], rules: dict[str, Any], cfg: dict[str, Any] | None = None
) -> bool:
    """Decide whether a candidate may enter the human review queue.

    Queue admission is deliberately stricter than the audit: by default a
    candidate needs *both* an AI term and an imaging-modality term, and
    Nature news/commentary DOIs are never queued (they stay in the raw
    audit).  Everything else remains visible in the audit artifact.
    """
    cfg = cfg if cfg is not None else discovery_config(rules)
    if result.get("decision") != "needs_review":
        return False
    if result.get("review_priority") not in ("high", "medium"):
        return False
    if result.get("ledger_status"):
        return False
    if result.get("date_status") not in (
            "verified", "conflict", "unresolved", "pubmed_electronic_only", "not_verified"):
        return False
    if bool(cfg.get("queue_requires_ai_and_modality", False)):
        signals = result.get("signals") or {}
        if not (signals.get("ai_title") or signals.get("ai_abstract")):
            return False
        if not (signals.get("modality_title") or signals.get("modality_abstract")):
            return False
    doi = curator.normalize_doi(str((result.get("metadata") or {}).get("doi") or ""))
    prefixes = tuple(str(prefix).casefold() for prefix in (cfg.get("queue_excluded_doi_prefixes") or []))
    if prefixes and doi and doi.casefold().startswith(prefixes):
        return False
    return True


def first_online_gate(
    first_online: str,
    *,
    start: dt.date,
    full_year: bool,
    target_year: int,
) -> tuple[str, str, str]:
    """Apply target-year and rolling-window rules to a verified first-online month.

    Returns (decision_override, date_status, reason); an empty decision means
    "keep the current decision" (the candidate stays under review).
    """
    if not first_online:
        return "", "unresolved", ""
    year = int(first_online[:4])
    if year != target_year:
        status = "before_target" if year < target_year else "after_target"
        return ("exclude", status,
                f"First-online year {year} is outside the {target_year} target "
                f"(verified; provenance stored).")
    if not full_year and int(first_online) < (start.year * 100 + start.month):
        return ("exclude", "outside_window",
                f"Verified first-online month {first_online} precedes the rolling "
                f"window start month {start.year}{start.month:02d}.")
    return "", "", ""


def run_discovery_v2(
    *,
    repo_root: Path,
    rules: dict[str, Any],
    source: str = "all",
    queue_limit: int | None = None,
    window_days: int | None = None,
    overlap_days: int | None = None,
    full_year: bool = False,
    ledger_path: Path | None = None,
    prefix: str | Path = ".curator/weekly",
    run_date: dt.date | None = None,
    catalog_index: dict[str, list[str]] | None = None,
    fetch_json=None,
    fetch_text=None,
    date_verify: bool = True,
    known_dois: list[str] | None = None,
) -> dict[str, Any]:
    cfg = discovery_config(rules)
    target_year = int(rules.get("target_year", 2026))
    start, end = rolling_range(rules, run_date=run_date, full_year=full_year,
                               window_days=window_days, overlap_days=overlap_days)

    index = catalog_index if catalog_index is not None else curator.parse_catalog(
        repo_root / rules.get("target_page", "biomedical_images.md"))

    resolved_ledger = Path(ledger_path) if ledger_path else repo_root / cfg.get(
        "ledger_relative", ".agents/skills/biomedical-image-curator/assets/reviewed-papers.json")
    ledger = load_ledger(resolved_ledger)
    suppressed = ledger_suppressed_statuses(ledger)

    retmax = int(cfg.get("pass_retmax", 100))
    batch_size = int(cfg.get("efetch_batch_size", 100))
    raw: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    pmid_order: list[str] = []

    queries = pubmed_queries_v2(rules, start, end) if source in ("all", "pubmed") else {}
    if source in ("all", "pubmed"):
        for pass_name in ("pubmed_p1", "pubmed_p2"):
            try:
                ids, records = run_pubmed_pass(rules, start, end, pass_name,
                                               queries[pass_name], retmax,
                                               fetch_json=fetch_json)
            except curator.CuratorError as exc:
                errors[pass_name] = str(exc)
                continue
            pmid_order.extend(ids)
            raw.extend(records)

    if source in ("all", "openalex"):
        records, openalex_error = openalex_sweep(rules, start, end,
                                                 fetch_json=fetch_json,
                                                 email=ncbi_email())
        if openalex_error:
            errors["openalex"] = openalex_error
        else:
            raw.extend(records)

    if pmid_order:
        abstracts = fetch_abstract_records(list(dict.fromkeys(pmid_order)),
                                           fetch_text=fetch_text,
                                           email=ncbi_email(),
                                           batch_size=batch_size)
        attach_abstracts(raw, abstracts)

    merged = curator.deduplicate_discovery(raw)

    results: list[dict[str, Any]] = []
    for metadata in merged:
        metadata["journal"] = canonical_journal(metadata.get("journal"), rules)
        signals = scope_signals(metadata, rules)
        evaluation = curator.evaluate_metadata(metadata, index, rules)
        decision = evaluation["decision"]
        reasons = list(evaluation["reasons"])
        priority = signals["review_priority"]
        if decision == "needs_review":
            priority = signals["review_priority"]
        date_status = "not_verified"
        first_online = month_code(metadata.get("date"))
        provenance: list[dict[str, Any]] = []
        candidate = metadata.get("pmid") and f"pmid-{metadata.get('pmid')}"
        doi = curator.normalize_doi(str(metadata.get("doi") or ""))
        candidate_id = candidate or (doi.split("/")[-1] if doi else slugify(metadata.get("title") or "candidate"))

        if decision == "needs_review" and priority in ("high", "medium") and date_verify:
            date_info = verify_first_online(metadata, fetch_json=fetch_json, fetch_text=fetch_text)
            provenance = date_info.get("provenance", [])
            first_online = date_info.get("first_online", "")
            status = date_info.get("date_status", "unresolved")
            if first_online:
                metadata["date"] = first_online
                metadata["first_online"] = first_online
                gate_decision, gate_status, gate_reason = first_online_gate(
                    first_online, start=start, full_year=full_year, target_year=target_year)
                if gate_decision == "exclude":
                    decision = "exclude"
                    date_status = gate_status
                    reasons.append(gate_reason)
                else:
                    date_status = status
                    if status == "conflict":
                        reasons.append(
                            "First-online sources conflict; manual review required "
                            "(see date provenance).")
            else:
                date_status = "unresolved"
                reasons.append("First-online month unresolved after publisher/Crossref/"
                               "PubMed checks; manual verification required.")
        elif decision == "needs_review":
            epub_code = month_code(metadata.get("epubdate"))
            if epub_code:
                date_status = "pubmed_electronic_only"
                first_online = epub_code or first_online
            else:
                date_status = "coarse_only"

        ledger_status = candidate_ledger_status(metadata, suppressed)
        if ledger_status and decision == "needs_review":
            reasons.append(f"Already decided in reviewed-papers ledger ({ledger_status}).")

        results.append({
            "candidate_id": candidate_id,
            "metadata": metadata,
            "decision": decision,
            "review_priority": priority,
            "signals": signals,
            "reasons": reasons,
            "gates": evaluation["gates"],
            "duplicates": evaluation["duplicates"],
            "date_status": date_status,
            "first_online": first_online,
            "date_provenance": provenance,
            "ledger_status": ledger_status,
        })

    decision_counts: dict[str, int] = {}
    priority_counts: dict[str, int] = {}
    date_status_counts: dict[str, int] = {}
    for result in results:
        decision_counts[result["decision"]] = decision_counts.get(result["decision"], 0) + 1
        priority_counts[result["review_priority"]] = priority_counts.get(result["review_priority"], 0) + 1
        date_status_counts[result["date_status"]] = date_status_counts.get(result["date_status"], 0) + 1

    suppressable = [
        result for result in results
        if result["decision"] == "needs_review" and result["review_priority"] in ("high", "medium")
    ]
    ledger_suppressed = sum(1 for result in suppressable if result["ledger_status"])
    queue_candidates = [result for result in suppressable if queue_eligible(result, rules, cfg)]

    signal_mix = {"both": 0, "ai_only": 0, "modality_only": 0, "neither": 0}
    for result in results:
        if result["decision"] != "needs_review":
            continue
        signals = result["signals"]
        has_ai = bool(signals.get("ai_title") or signals.get("ai_abstract"))
        has_modality = bool(signals.get("modality_title") or signals.get("modality_abstract"))
        if has_ai and has_modality:
            signal_mix["both"] += 1
        elif has_ai:
            signal_mix["ai_only"] += 1
        elif has_modality:
            signal_mix["modality_only"] += 1
        else:
            signal_mix["neither"] += 1
    queue_admission = {
        "require_ai_and_modality": bool(cfg.get("queue_requires_ai_and_modality", False)),
        "excluded_doi_prefixes": list(cfg.get("queue_excluded_doi_prefixes") or []),
    }
    queue_candidates.sort(key=lambda item: (0 if item["review_priority"] == "high" else 1,
                                            item["first_online"] or "", item["candidate_id"]))
    limit = int(queue_limit) if queue_limit is not None else int(cfg.get("scheduled_queue_limit", 10))
    queued = queue_candidates[:limit] if limit > 0 else queue_candidates

    findings = [{
        "doi": curator.normalize_doi(str(result["metadata"].get("doi") or "")),
        "title": result["metadata"].get("title") or "",
        "journal": result["metadata"].get("journal") or "",
        "decision": result["decision"],
        "priority": result["review_priority"],
        "retrieval_pass": result["metadata"].get("retrieval_pass") or "",
        "first_online": result.get("first_online") or "",
    } for result in results]

    payload: dict[str, Any] = {
        "run": {
            "date": (run_date or today_utc()).isoformat(),
            "window": f"{start.isoformat()} .. {end.isoformat()}",
            "full_year": bool(full_year),
            "sources": [source] if source != "all" else ["PubMed (pass 1)", "PubMed (pass 2)", "OpenAlex"],
            "queue_limit": limit,
            "target_year": target_year,
        },
        "ledger": {"path": str(resolved_ledger), "entries": len(ledger.get("entries", []))},
        "source_errors": errors,
        "counts": {"total": len(results), **decision_counts},
        "priority_counts": priority_counts,
        "date_status_counts": date_status_counts,
        "signal_mix": signal_mix,
        "queue_admission": queue_admission,
        "ledger_suppressed": ledger_suppressed,
        "queue_size": len(queued),
        "queue": [queue_entry(result) for result in queued],
        "results": [{
            "candidate_id": result["candidate_id"],
            "metadata": {**result["metadata"], "abstract": truncate(result["metadata"].get("abstract") or "", 2000)},
            "decision": result["decision"],
            "review_priority": result["review_priority"],
            "signals": result["signals"],
            "reasons": result["reasons"],
            "gates": result["gates"],
            "duplicates": result["duplicates"],
            "date_status": result["date_status"],
            "first_online": result["first_online"],
            "date_provenance": result["date_provenance"],
            "ledger_status": result["ledger_status"],
        } for result in results],
        "findings": findings,
    }

    if known_dois:
        expected = {curator.normalize_doi(value) for value in known_dois if curator.normalize_doi(value)}
        payload["benchmark"] = benchmark_recall(raw, merged, expected)

    # Persist outputs. Absolute paths are used for writing; the reported file
    # names keep the caller's prefix so summaries read naturally.
    base = Path(prefix)
    if not base.is_absolute():
        base = repo_root / base
    base.parent.mkdir(parents=True, exist_ok=True)
    suffixes = {
        "queue_json": "-review-queue.json",
        "queue_md": "-review-queue.md",
        "audit_json": "-raw-audit.json",
        "summary_md": "-summary.md",
    }
    display_prefix = Path(prefix).as_posix()
    payload["files"] = {
        key: (display_prefix + suffix) for key, suffix in suffixes.items()
    }
    absolute_paths = {
        key: Path(str(base) + suffix) for key, suffix in suffixes.items()
    }
    write_json(absolute_paths["queue_json"], {
        "run": payload["run"], "counts": payload["counts"],
        "ledger_suppressed": ledger_suppressed, "queue": payload["queue"],
    })
    absolute_paths["queue_md"].write_text(render_queue_markdown(payload, payload["queue"]), encoding="utf-8")
    write_json(absolute_paths["audit_json"], {
        "run": payload["run"],
        "ledger": payload["ledger"],
        "source_errors": errors,
        "counts": payload["counts"],
        "priority_counts": priority_counts,
        "date_status_counts": date_status_counts,
        "results": payload["results"],
    })
    absolute_paths["summary_md"].write_text(render_summary_markdown(payload, payload["queue"]), encoding="utf-8")
    return payload


def benchmark_recall(raw: list[dict[str, Any]], merged: list[dict[str, Any]], expected: set[str]) -> dict[str, Any]:
    def doi_set(records: list[dict[str, Any]]) -> set[str]:
        return {curator.normalize_doi(str(record.get("doi") or "")) for record in records
                if curator.normalize_doi(str(record.get("doi") or ""))}
    found = doi_set(merged)
    by_pass: dict[str, set[str]] = {}
    for record in raw:
        doi = curator.normalize_doi(str(record.get("doi") or ""))
        if not doi:
            continue
        passes = record.get("retrieval_pass") or []
        if isinstance(passes, str):
            passes = [passes]
        for item in passes:
            by_pass.setdefault(item, set()).add(doi)
    return {
        "expected": len(expected),
        "found_any_source": len(found & expected),
        "not_found": sorted(expected - found),
        "found_by_pass": {pass_name: len(hits & expected) for pass_name, hits in by_pass.items()},
    }


# ---------------------------------------------------------------------------
# AI first-pass review (optional, read-only, hard-capped)
# ---------------------------------------------------------------------------

AI_DECISIONS = {"include", "exclude", "needs_human_review"}
AI_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "env_api_key": "DEEPSEEK_API_KEY",
    "max_candidates_per_run": 10,
    "max_requests_per_run": 12,
    "max_retries": 1,
    "retry_seconds": 2,
    "max_abstract_chars": 1800,
    "max_input_chars_per_candidate": 6000,
    "max_output_tokens": 700,
    "temperature": 0,
    "thinking": "disabled",
    "timeout_seconds": 60,
    "price_input_per_mtok": 0.3,
    "price_output_per_mtok": 1.2,
}


def ai_config(rules: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(AI_DEFAULT_CONFIG)
    cfg.update(rules.get("ai") or {})
    return cfg


def scrub_secrets(text: Any) -> str:
    """Remove anything that looks like an API key before it can be logged."""
    return re.sub(r"sk-[A-Za-z0-9_\-]{4,}", "sk-***", str(text or ""))


def ai_policy_brief(rules: dict[str, Any]) -> str:
    journals = ", ".join(rules.get("priority_journals") or [])
    modalities = ", ".join(rules.get("modality_terms") or [])
    excluded = ", ".join(rules.get("excluded_domains") or [])
    return (
        f"Target: papers first published online in {rules.get('target_year')} in a Nature Portfolio "
        f"journal whose displayed name begins with 'Nature' (priority journals: {journals}).\n"
        "In scope: the paper's central contribution is an important AI model, system or image-analysis "
        f"method whose primary data are biomedical images in these modalities: {modalities}.\n"
        f"Out of scope: {excluded}; conference or IEEE papers; non-Nature venues; preprints used instead "
        "of the published article; pathology-dominant, CT/MRI radiology, EHR/longitudinal or general "
        "LLM/multimodal work; papers where AI is only a minor analysis tool."
    )


def ai_output_contract() -> str:
    return (
        "Reply with exactly one json object and nothing else (no markdown fences). Required shape:\n"
        '{"candidate_id": "<echo the candidate_id>", "decision": "include" | "exclude" | '
        '"needs_human_review", "confidence": 0.0, "reason": "one or two sentences", '
        '"scope_category": "short label", "date_status": "verified | unresolved | conflict | implausible", '
        '"evidence_urls": ["only urls listed in known_urls"], "uncertainties": ["short items"]}'
    )


def candidate_urls(candidate: dict[str, Any]) -> list[str]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else candidate
    urls = []
    for value in (candidate.get("doi_url"), candidate.get("paper_url"),
                  metadata.get("doi_url"), metadata.get("paper_url")):
        if value:
            urls.append(str(value))
    doi = curator.normalize_doi(str(metadata.get("doi") or candidate.get("doi") or ""))
    if doi:
        urls.append(f"https://doi.org/{doi}")
    return sorted({url.rstrip("/") for url in urls})


def build_ai_messages(
    candidate: dict[str, Any], rules: dict[str, Any], cfg: dict[str, Any]
) -> tuple[list[dict[str, str]], list[str]]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else candidate
    allowed = candidate_urls(candidate)
    payload = {
        "candidate_id": candidate.get("candidate_id"),
        "title": metadata.get("title") or candidate.get("title") or "",
        "journal": metadata.get("journal") or candidate.get("journal") or "",
        "first_online": candidate.get("first_online") or "",
        "date_status": candidate.get("date_status") or "",
        "retrieval_pass": metadata.get("retrieval_pass") or candidate.get("retrieval_pass") or "",
        "abstract": truncate(str(metadata.get("abstract") or candidate.get("abstract") or ""),
                             int(cfg["max_abstract_chars"])),
        "known_urls": allowed,
    }
    system = (
        "You screen candidate papers for a public catalogue page called 'Biomedical Images - Other'. "
        "You only judge scope relevance and date plausibility; a human makes every final decision, so "
        "use needs_human_review whenever the candidate is ambiguous.\n\n"
        + ai_policy_brief(rules) + "\n\n" + ai_output_contract()
    )
    user = "Candidate json:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    max_chars = int(cfg["max_input_chars_per_candidate"])
    if len(user) > max_chars:
        user = user[:max_chars]
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], allowed


def parse_ai_reply(text: Any, candidate_id: str, allowed_urls: list[str]) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise curator.CuratorError("empty response content")
    if raw.startswith("```"):
        raw = re.sub(r"^```[A-Za-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise curator.CuratorError(f"invalid json: {exc}") from exc
    if not isinstance(data, dict):
        raise curator.CuratorError("response is not a json object")
    if str(data.get("candidate_id") or "") != str(candidate_id):
        raise curator.CuratorError("candidate_id mismatch")
    decision = str(data.get("decision") or "").strip()
    if decision not in AI_DECISIONS:
        raise curator.CuratorError(f"invalid decision '{decision}'")
    reason = truncate(str(data.get("reason") or "").strip(), 600)
    if not reason:
        raise curator.CuratorError("reason is required")
    confidence: float | None
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence"))))
    except (TypeError, ValueError):
        confidence = None
    allowed = {url.rstrip("/") for url in allowed_urls}
    kept: list[str] = []
    dropped = 0
    for url in data.get("evidence_urls") if isinstance(data.get("evidence_urls"), list) else []:
        value = str(url).strip()
        if not value:
            continue
        if value.rstrip("/") in allowed:
            kept.append(value)
        else:
            dropped += 1
    uncertainties = [truncate(str(item).strip(), 300)
                     for item in (data.get("uncertainties") or [])
                     if str(item).strip()][:6]
    if dropped:
        uncertainties.append(f"Dropped {dropped} evidence URL(s) that were not in the candidate record.")
    return {
        "candidate_id": str(candidate_id),
        "decision": decision,
        "confidence": confidence,
        "reason": reason,
        "scope_category": truncate(str(data.get("scope_category") or "").strip(), 120),
        "date_status": truncate(str(data.get("date_status") or "").strip(), 60),
        "evidence_urls": kept[:5],
        "uncertainties": uncertainties[:6],
    }


def _default_ai_transport(url: str, headers: dict[str, str], body: bytes, timeout: int) -> dict[str, Any]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # pragma: no cover - defensive
            detail = ""
        raise curator.CuratorError(f"DeepSeek API HTTP {exc.code}: {scrub_secrets(detail)}") from exc
    except (URLError, TimeoutError) as exc:
        raise curator.CuratorError(f"DeepSeek API request failed: {scrub_secrets(exc)}") from exc
    except json.JSONDecodeError as exc:
        raise curator.CuratorError(f"DeepSeek API returned invalid json: {exc}") from exc


def _accumulate_usage(usage: dict[str, int], response: Any) -> None:
    if not isinstance(response, dict):
        return
    reported = response.get("usage")
    if not isinstance(reported, dict):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = reported.get(key)
        if isinstance(value, (int, float)):
            usage[key] += int(value)


def estimate_ai_cost_usd(usage: dict[str, int], cfg: dict[str, Any]) -> float:
    return round(
        usage.get("prompt_tokens", 0) / 1_000_000 * float(cfg["price_input_per_mtok"])
        + usage.get("completion_tokens", 0) / 1_000_000 * float(cfg["price_output_per_mtok"]),
        6,
    )


def render_ai_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# AI first-pass review (DeepSeek)",
        "",
        f"**Run:** {payload['run']['date']} · **Model:** `{payload['model']}` · **Status:** `{payload['status']}`",
        f"**Candidates:** reviewed {payload['reviewed']} of {payload['candidates_available']} queued "
        f"(caps: {payload['budget']['max_candidates_per_run']} candidates / "
        f"{payload['budget']['max_requests_per_run']} requests per run)",
        f"**Usage:** {payload['usage']['total_tokens']} tokens over {payload['usage']['requests']} request(s) · "
        f"estimated cost ≈ ${payload['estimated_cost_usd']:.4f} (upper-bound peak pricing)",
        "",
        "> The AI only advises. A maintainer must still verify the paper and record the final decision in "
        "`reviewed-papers.json` (`accepted` / `excluded` / `other_page`) before any catalogue change.",
        "",
    ]
    if payload["status"] != "ok":
        lines.append(f"_No API call was made: `{payload['status']}`._")
        lines.append("")
    for item in payload["results"]:
        lines.append(f"## {item.get('candidate_id')}")
        lines.append("")
        if item.get("title"):
            lines.append(f"**{item['title']}**")
            lines.append("")
        if item.get("doi"):
            lines.append(f"- **DOI:** [{item['doi']}](https://doi.org/{item['doi']})")
        if item.get("journal"):
            lines.append(f"- **Journal:** {item['journal']}")
        confidence = item.get("confidence")
        lines.append(f"- **AI decision:** `{item.get('decision')}`"
                     + (f" (confidence {confidence:.2f})" if isinstance(confidence, (int, float)) else ""))
        if item.get("scope_category"):
            lines.append(f"- **Scope category:** {item['scope_category']}")
        if item.get("date_status"):
            lines.append(f"- **Date status:** {item['date_status']}")
        if item.get("reason"):
            lines.append(f"- **Reason:** {item['reason']}")
        if item.get("evidence_urls"):
            lines.append("- **Evidence:** " + " · ".join(item["evidence_urls"]))
        if item.get("uncertainties"):
            lines.append("- **Uncertainties:** " + "; ".join(item["uncertainties"]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_ai_review(
    *,
    repo_root: Path,
    rules: dict[str, Any],
    queue: dict[str, Any] | None = None,
    queue_path: str | Path | None = None,
    prefix: str | Path = ".curator/weekly",
    transport: Callable[..., dict[str, Any]] | None = None,
    api_key: str | None = None,
    run_date: dt.date | None = None,
    max_candidates: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = ai_config(rules)
    if queue is None:
        candidate_path = Path(queue_path) if queue_path else Path(f"{prefix}-review-queue.json")
        if not candidate_path.is_absolute():
            candidate_path = repo_root / candidate_path
        if not candidate_path.exists():
            raise curator.CuratorError(f"Review queue not found: {candidate_path}")
        try:
            queue = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise curator.CuratorError(f"Could not read review queue {candidate_path}: {exc}") from exc
    candidates = [item for item in (queue.get("queue") or []) if isinstance(item, dict)]
    hard_cap = int(cfg["max_candidates_per_run"])
    limit = hard_cap if max_candidates is None else min(int(max_candidates), hard_cap)
    selected = candidates[:limit]

    key = (api_key if api_key is not None else os.environ.get(str(cfg["env_api_key"]), "")).strip()
    if dry_run:
        status = "dry_run"
    elif not bool(cfg.get("enabled", True)):
        status = "disabled"
    elif not key:
        status = "skipped_no_api_key"
    else:
        status = "ok"

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "requests": 0}
    counts = {"include": 0, "exclude": 0, "needs_human_review": 0, "not_reviewed": 0}
    results: list[dict[str, Any]] = []
    url = str(cfg["base_url"]).rstrip("/") + "/chat/completions"
    max_requests = int(cfg["max_requests_per_run"])

    for index, candidate in enumerate(selected):
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else candidate
        candidate_id = str(candidate.get("candidate_id") or f"candidate-{index + 1}")
        doi = curator.normalize_doi(str(metadata.get("doi") or candidate.get("doi") or ""))
        entry: dict[str, Any] = {
            "candidate_id": candidate_id,
            "title": metadata.get("title") or candidate.get("title") or "",
            "journal": metadata.get("journal") or candidate.get("journal") or "",
            "doi": doi,
            "decision": "not_reviewed",
            "confidence": None,
            "reason": "",
            "scope_category": "",
            "date_status": candidate.get("date_status") or "",
            "evidence_urls": [],
            "uncertainties": [],
        }
        messages, allowed = build_ai_messages(candidate, rules, cfg)
        if status != "ok":
            entry["reason"] = f"AI review {status}; no API call was made."
            if status == "dry_run":
                entry["prompt_preview"] = messages[1]["content"][:400]
            counts["not_reviewed"] += 1
            results.append(entry)
            continue
        if usage["requests"] >= max_requests:
            entry["reason"] = "Per-run request cap reached; not reviewed."
            entry["uncertainties"] = ["Increase ai.max_requests_per_run to review more candidates."]
            counts["not_reviewed"] += 1
            results.append(entry)
            continue

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        reviewed: dict[str, Any] | None = None
        last_error = ""
        attempts = int(cfg["max_retries"]) + 1
        for attempt in range(attempts):
            if usage["requests"] >= max_requests:
                last_error = "Per-run request cap reached."
                break
            attempt_messages = list(messages)
            if attempt and last_error:
                attempt_messages = messages + [{
                    "role": "user",
                    "content": f"Your previous answer was rejected ({last_error}). "
                               "Return only the corrected json object.",
                }]
            body = json.dumps({
                "model": cfg["model"],
                "messages": attempt_messages,
                "temperature": float(cfg["temperature"]),
                "max_tokens": int(cfg["max_output_tokens"]),
                "response_format": {"type": "json_object"},
                "thinking": {"type": str(cfg.get("thinking") or "disabled")},
                "stream": False,
            }, ensure_ascii=False).encode("utf-8")
            usage["requests"] += 1
            try:
                response = (transport or _default_ai_transport)(
                    url, headers, body, int(cfg["timeout_seconds"]))
                _accumulate_usage(usage, response)
                content = ""
                choices = response.get("choices") if isinstance(response, dict) else None
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    message = choices[0].get("message")
                    if isinstance(message, dict):
                        content = message.get("content") or ""
                reviewed = parse_ai_reply(content, candidate_id, allowed)
                break
            except curator.CuratorError as exc:
                last_error = scrub_secrets(exc)
                if attempt + 1 < attempts and usage["requests"] < max_requests:
                    time.sleep(float(cfg.get("retry_seconds", 2.0)))
        if reviewed is None:
            entry["decision"] = "needs_human_review"
            entry["reason"] = f"AI review failed: {last_error or 'unknown error'}"
            entry["uncertainties"] = ["AI screening failed; manual review required."]
            entry["error"] = True
            counts["needs_human_review"] += 1
        else:
            entry.update(reviewed)
            counts[entry["decision"]] = counts.get(entry["decision"], 0) + 1
        results.append(entry)

    reviewed_count = sum(1 for item in results
                         if item.get("decision") != "not_reviewed" and not item.get("error"))
    absolute_prefix = Path(prefix)
    if not absolute_prefix.is_absolute():
        absolute_prefix = repo_root / absolute_prefix
    absolute_prefix.parent.mkdir(parents=True, exist_ok=True)
    absolute = {
        "ai_json": Path(str(absolute_prefix) + "-ai-review.json"),
        "ai_md": Path(str(absolute_prefix) + "-ai-review.md"),
    }
    display_prefix = Path(prefix).as_posix()
    payload: dict[str, Any] = {
        "run": {"date": (run_date or today_utc()).isoformat()},
        "status": status,
        "model": str(cfg["model"]),
        "candidates_available": len(candidates),
        "reviewed": reviewed_count,
        "budget": {
            "max_candidates_per_run": hard_cap,
            "max_requests_per_run": max_requests,
            "max_output_tokens": int(cfg["max_output_tokens"]),
            "max_input_chars_per_candidate": int(cfg["max_input_chars_per_candidate"]),
        },
        "usage": usage,
        "estimated_cost_usd": estimate_ai_cost_usd(usage, cfg),
        "counts": counts,
        "results": results,
        "files": {key_name: display_prefix + suffix for key_name, suffix in
                  (("ai_json", "-ai-review.json"), ("ai_md", "-ai-review.md"))},
    }
    write_json(absolute["ai_json"], {
        "run": payload["run"], "status": status, "model": payload["model"],
        "budget": payload["budget"], "usage": usage,
        "estimated_cost_usd": payload["estimated_cost_usd"],
        "counts": counts, "results": results,
    })
    absolute["ai_md"].write_text(render_ai_markdown(payload), encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# CLI entry points (thin wrappers so curator.py stays small)
# ---------------------------------------------------------------------------

def run_discovery_v2_cli(args: Any, repo_root: Path) -> None:
    rules = curator.load_rules()
    payload = run_discovery_v2(
        repo_root=repo_root,
        rules=rules,
        source=args.source,
        queue_limit=args.queue_limit,
        window_days=args.window_days,
        overlap_days=args.overlap_days,
        full_year=args.full_year,
        ledger_path=args.ledger,
        prefix=args.prefix,
    )
    print(json.dumps({
        "window": payload["run"]["window"],
        "queue_size": payload["queue_size"],
        "queue_limit": payload["run"]["queue_limit"],
        "counts": payload["counts"],
        "priority_counts": payload["priority_counts"],
        "date_status_counts": payload["date_status_counts"],
        "ledger_suppressed": payload["ledger_suppressed"],
        "source_errors": payload["source_errors"],
        "files": payload["files"],
    }, ensure_ascii=False, indent=2))


def validate_ledger_cli(args: Any, repo_root: Path) -> None:
    rules = curator.load_rules()
    cfg = discovery_config(rules)
    if args.ledger:
        path = Path(args.ledger)
    else:
        path = repo_root / cfg.get("ledger_relative",
                                   ".agents/skills/biomedical-image-curator/assets/reviewed-papers.json")
    if not path.exists():
        raise curator.CuratorError(f"Ledger file does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise curator.CuratorError(f"Could not read ledger {path}: {exc}") from exc
    errors = validate_ledger(data)
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


def ai_review_cli(args: Any, repo_root: Path) -> None:
    rules = curator.load_rules()
    payload = run_ai_review(
        repo_root=repo_root,
        rules=rules,
        queue_path=args.queue,
        prefix=args.prefix,
        max_candidates=args.max_candidates,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps({
        "status": payload["status"],
        "model": payload["model"],
        "candidates_available": payload["candidates_available"],
        "reviewed": payload["reviewed"],
        "counts": payload["counts"],
        "usage": payload["usage"],
        "estimated_cost_usd": payload["estimated_cost_usd"],
        "files": payload["files"],
    }, ensure_ascii=False, indent=2))
