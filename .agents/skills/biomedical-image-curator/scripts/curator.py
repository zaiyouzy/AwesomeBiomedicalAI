#!/usr/bin/env python3
"""Deterministic helpers for the Biomedical Images — Other curator skill."""

from __future__ import annotations

import argparse
import datetime as dt
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


SKILL_DIR = Path(__file__).resolve().parents[1]
RULES_PATH = SKILL_DIR / "assets" / "curation-rules.json"
USER_AGENT = "AwesomeBiomedicalAI-curator/0.1 (+https://github.com/medfm-flare/AwesomeBiomedicalAI)"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{6}$")
MODEL_SIZE_STATUSES = {
    "reported", "computed_weights", "computed_code", "not_reported", "not_publicly_verifiable"
}
RESOURCE_LABELS = {
    "Code", "Weights", "Data", "Project page / demo", "Code / models", "Code / weights",
    "Implementation", "Reproducibility code", "Original research code", "Interactive tool",
    "Interactive data", "Weights / example data",
}


class CuratorError(RuntimeError):
    """A user-actionable curator failure."""


class CitationMetaParser(HTMLParser):
    """Collect publisher citation meta tags without external dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        name = (values.get("name") or values.get("property") or "").lower()
        content = values.get("content")
        if name and content:
            self.meta.setdefault(name, []).append(html.unescape(content).strip())


def load_rules() -> dict[str, Any]:
    with RULES_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def find_repo_root(start: Path | None = None) -> Path:
    candidates = [start.resolve()] if start else []
    candidates.extend(Path(__file__).resolve().parents)
    candidates.append(Path.cwd().resolve())
    for candidate in candidates:
        if candidate.is_file():
            candidate = candidate.parent
        for parent in (candidate, *candidate.parents):
            if (parent / "biomedical_images.md").is_file():
                return parent
    raise CuratorError("Could not locate a repository root containing biomedical_images.md")


def request_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            encoding = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(encoding, errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise CuratorError(f"Request failed for {url}: {exc}") from exc


def request_json(url: str, timeout: int = 30) -> dict[str, Any]:
    try:
        value = json.loads(request_text(url, timeout=timeout))
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("top-level JSON is not an object")
        return value
    except (json.JSONDecodeError, ValueError) as exc:
        raise CuratorError(f"Invalid JSON returned by {url}: {exc}") from exc


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    match = DOI_RE.search(html.unescape(value))
    return match.group(0).rstrip(".,;)]}").lower() if match else ""


def extract_doi(value: str) -> str:
    doi = normalize_doi(value)
    if doi:
        return doi
    match = re.search(r"/articles/(s\d[\w.-]+)", urlparse(value).path, flags=re.IGNORECASE)
    return f"10.1038/{match.group(1).lower()}" if match else ""


def normalize_identity(value: str | None) -> str:
    return "".join(character for character in (value or "").casefold() if character.isalnum())


def first(values: Any, default: str = "") -> str:
    if isinstance(values, list) and values:
        return str(values[0])
    return values if isinstance(values, str) else default


def date_from_parts(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict):
        return "", ""
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list) or not parts[0]:
        return "", ""
    year = int(parts[0][0])
    month = int(parts[0][1]) if len(parts[0]) > 1 else 0
    return (f"{year:04d}-{month:02d}" if month else f"{year:04d}"), (f"{year:04d}{month:02d}" if month else "")


def crossref_metadata(doi: str) -> dict[str, Any]:
    url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
    message = request_json(url).get("message")
    if not isinstance(message, dict):
        raise CuratorError(f"Crossref returned no work metadata for {doi}")
    publication_date = yyyymm = date_source = ""
    for key in ("published-online", "published", "issued", "published-print", "created"):
        publication_date, yyyymm = date_from_parts(message.get(key))
        if publication_date:
            date_source = f"Crossref {key}"
            break
    authors = []
    for author in message.get("author", []) if isinstance(message.get("author"), list) else []:
        if isinstance(author, dict):
            name = " ".join(part for part in (author.get("given", ""), author.get("family", "")) if part).strip()
            if name:
                authors.append(name)
    return {
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}",
        "title": html.unescape(first(message.get("title"))),
        "journal": html.unescape(first(message.get("container-title"))),
        "publication_date": publication_date,
        "date": yyyymm,
        "date_source": date_source,
        "authors": authors,
        "paper_url": message.get("URL") or f"https://doi.org/{doi}",
        "metadata_source": url,
    }


def publisher_metadata(url: str) -> dict[str, Any]:
    parser = CitationMetaParser()
    parser.feed(request_text(url))
    meta = parser.meta
    publication = first(meta.get("citation_online_date")) or first(meta.get("citation_publication_date"))
    date = ""
    numbers = re.findall(r"\d+", publication)
    if numbers:
        year = int(numbers[0])
        month = int(numbers[1]) if len(numbers) > 1 and 1 <= int(numbers[1]) <= 12 else 0
        date = f"{year:04d}{month:02d}" if month else ""
    return {
        "doi": normalize_doi(first(meta.get("citation_doi"))),
        "title": first(meta.get("citation_title")) or first(meta.get("dc.title")),
        "journal": first(meta.get("citation_journal_title")),
        "publication_date": publication,
        "date": date,
        "date_source": "publisher citation metadata",
        "authors": meta.get("citation_author", []),
        "paper_url": url,
        "metadata_source": url,
    }


def resolve_metadata(value: str) -> dict[str, Any]:
    value = value.strip()
    doi = extract_doi(value)
    publisher: dict[str, Any] = {}
    if value.lower().startswith(("http://", "https://")):
        try:
            publisher = publisher_metadata(value)
            doi = publisher.get("doi") or doi
        except CuratorError:
            if not doi:
                raise
    if doi:
        try:
            metadata = crossref_metadata(doi)
        except CuratorError:
            if not publisher:
                raise
            metadata = publisher
        for key, publisher_value in publisher.items():
            if publisher_value and (not metadata.get(key) or key in {"paper_url", "date", "publication_date", "date_source"}):
                metadata[key] = publisher_value
        metadata["doi"] = doi
        metadata["doi_url"] = f"https://doi.org/{doi}"
        if value.lower().startswith(("http://", "https://")):
            metadata["paper_url"] = value
        return metadata
    if publisher:
        return publisher
    raise CuratorError(f"Could not resolve a DOI or publisher metadata from: {value}")


def parse_catalog(catalog_path: Path) -> dict[str, list[str]]:
    text = catalog_path.read_text(encoding="utf-8")
    dois = {normalize_doi(match.group(0)) for match in DOI_RE.finditer(text) if normalize_doi(match.group(0))}
    for url in re.findall(r"https?://[^)\s>]+", text):
        doi = extract_doi(url.rstrip(".,;\"'"))
        if doi:
            dois.add(doi)
    dois = sorted(dois)
    titles = sorted({
        match.strip()
        for match in re.findall(r"\*\*\[([^\]]+)\]\(https?://[^)]+\)\*\*", text)
    })
    models = sorted({
        html.unescape(match.strip())
        for match in re.findall(r"<summary><b>([^<]+)</b>", text)
    })
    return {"dois": dois, "titles": titles, "models": models}


def duplicate_matches(
    index: dict[str, list[str]], doi: str = "", title: str = "", model: str = ""
) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    if doi and normalize_doi(doi) in index["dois"]:
        matches.append({"field": "doi", "value": normalize_doi(doi)})
    normalized_title = normalize_identity(title)
    for existing in index["titles"]:
        if normalized_title and normalize_identity(existing) == normalized_title:
            matches.append({"field": "title", "value": existing})
            break
    normalized_model = normalize_identity(model)
    for existing in index["models"]:
        if normalized_model and normalize_identity(existing) == normalized_model:
            matches.append({"field": "model_name", "value": existing})
            break
    return matches


def metadata_year(metadata: dict[str, Any]) -> int | None:
    date = str(metadata.get("date") or metadata.get("publication_date") or "")
    match = re.search(r"(?:19|20)\d{2}", date)
    return int(match.group(0)) if match else None


def matching_terms(text: str, terms: list[str]) -> list[str]:
    lowered = text.casefold()
    hits = []
    for term in terms:
        candidate = term.casefold()
        if len(candidate) <= 3:
            matched = bool(re.search(rf"\b{re.escape(candidate)}\b", lowered))
        else:
            matched = candidate in lowered
        if matched:
            hits.append(term)
    return hits


def title_review_priority(metadata: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    title = str(metadata.get("title") or "")
    ai_hits = matching_terms(title, rules["ai_terms"])
    modality_hits = matching_terms(title, rules["modality_terms"])
    excluded_hits = matching_terms(title, rules["excluded_domains"])
    if excluded_hits:
        priority = "other_page"
    elif ai_hits and modality_hits:
        priority = "high"
    elif ai_hits or modality_hits:
        priority = "medium"
    else:
        priority = "low"
    return {
        "review_priority": priority,
        "title_ai_terms": ai_hits,
        "title_modality_terms": modality_hits,
        "title_other_page_terms": excluded_hits,
        "advisory": True,
    }


def evaluate_metadata(
    metadata: dict[str, Any],
    index: dict[str, list[str]],
    rules: dict[str, Any],
    model_name: str = "",
) -> dict[str, Any]:
    year = metadata_year(metadata)
    journal = str(metadata.get("journal") or "").strip()
    prefixes = tuple(str(prefix).casefold() for prefix in rules["allowed_journal_prefixes"])
    journal_ok = bool(journal) and journal.casefold().startswith(prefixes)
    year_ok = year == int(rules["target_year"]) if year is not None else None
    duplicates = duplicate_matches(
        index,
        str(metadata.get("doi") or ""),
        str(metadata.get("title") or ""),
        model_name,
    )
    if duplicates:
        decision = "duplicate"
    elif year_ok is False or (journal and not journal_ok):
        decision = "exclude"
    else:
        decision = "needs_review"

    reasons = []
    if duplicates:
        reasons.append("A matching DOI, normalized title, or model name already exists in the catalogue.")
    if year_ok is False:
        reasons.append(f"Publication year {year} is outside the configured {rules['target_year']} discovery target.")
    if year is None:
        reasons.append("The first-online year still needs verification.")
    if journal and not journal_ok:
        reasons.append(f"Journal '{journal}' does not begin with an allowed Nature-family prefix.")
    if not journal:
        reasons.append("The journal still needs verification.")
    if decision == "needs_review":
        reasons.append("Year/journal gates passed or remain unresolved; semantic page scope and detailed evidence still require review.")
    title_signals = title_review_priority(metadata, rules)
    return {
        "decision": decision,
        "review_priority": title_signals["review_priority"],
        "title_signals": title_signals,
        "reasons": reasons,
        "gates": {
            "target_year": rules["target_year"],
            "publication_year": year,
            "year_ok": year_ok,
            "journal": journal,
            "journal_name_starts_with_allowed_prefix": journal_ok if journal else None,
            "semantic_scope": "needs_review" if decision == "needs_review" else "not_run",
        },
        "duplicates": duplicates,
    }


def record_skeleton(
    metadata: dict[str, Any], evaluation: dict[str, Any], model_name: str = ""
) -> dict[str, Any]:
    return {
        "decision": evaluation["decision"],
        "date": metadata.get("date") or "2026MM",
        "model_name": model_name,
        "title": metadata.get("title") or "",
        "paper_url": metadata.get("paper_url") or metadata.get("doi_url") or "",
        "doi": metadata.get("doi") or "",
        "venue": metadata.get("journal") or "",
        "authors": metadata.get("authors") or [],
        "model_type": "",
        "backbone": "",
        "model_size": {"text": "", "status": "not_reported", "evidence": []},
        "training_data": {"text": "", "evidence": []},
        "training_adaptation": {"text": "", "evidence": []},
        "downstream_tasks": {"text": "", "evidence": []},
        "modalities": [],
        "resources": [],
        "performance": [],
        "verification_note": "",
        "supplement_url": None,
        "notebooklm_url": None,
    }


def review_markdown(metadata: dict[str, Any], evaluation: dict[str, Any]) -> str:
    lines = [
        "# Candidate pre-check",
        "",
        f"- **Decision:** `{evaluation['decision']}`",
        f"- **Review priority:** `{evaluation['review_priority']}` (title-only advisory signal)",
        f"- **Title:** {metadata.get('title') or 'Unresolved'}",
        f"- **DOI:** {metadata.get('doi') or 'Unresolved'}",
        f"- **Journal:** {metadata.get('journal') or 'Unresolved'}",
        f"- **First-online date:** {metadata.get('publication_date') or metadata.get('date') or 'Needs verification'}",
        f"- **Metadata source:** {metadata.get('metadata_source') or 'Unresolved'}",
        "",
        "## Reasons",
        "",
    ]
    lines.extend(f"- {reason}" for reason in evaluation["reasons"])
    if evaluation["decision"] == "needs_review":
        lines.extend([
            "", "## Required next review", "",
            "- Decide whether the paper's primary contribution belongs in Biomedical Images — Other.",
            "- Verify the first-online month from the publisher.",
            "- Review article and supplement for training data, model size, training/adaptation, and performance.",
            "- Verify official Code, Weights, Data, and Project page/demo labels.",
        ])
    return "\n".join(lines).rstrip() + "\n"


def output_root(repo_root: Path) -> Path:
    root = repo_root / ".curator"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "paper"


def candidate_stem(metadata: dict[str, Any]) -> str:
    doi = str(metadata.get("doi") or "")
    return slugify(doi.split("/")[-1] if doi else str(metadata.get("title") or "paper"))[:80]


def pubmed_query(rules: dict[str, Any], year: int) -> str:
    journals = " OR ".join(f'"{journal}"[Journal]' for journal in rules["priority_journals"])
    ai_terms = " OR ".join(f'"{term}"[Title/Abstract]' for term in rules["ai_terms"])
    modalities = " OR ".join(f'"{term}"[Title/Abstract]' for term in rules["modality_terms"])
    dates = f'"{year}/01/01"[Date - Publication] : "{year}/12/31"[Date - Publication]'
    excluded_types = "Editorial[Publication Type] OR Comment[Publication Type] OR Letter[Publication Type] OR Published Erratum[Publication Type]"
    return f"(({ai_terms}) AND ({modalities})) AND ({journals}) AND ({dates}) NOT ({excluded_types})"


def pubmed_discover(rules: dict[str, Any], year: int, maximum: int) -> list[dict[str, Any]]:
    parameters = {
        "db": "pubmed", "retmode": "json", "retmax": str(maximum),
        "sort": "pub date", "term": pubmed_query(rules, year),
        "tool": "AwesomeBiomedicalAI-curator",
    }
    email = os.environ.get("NCBI_EMAIL", "").strip()
    if email:
        parameters["email"] = email
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urlencode(parameters)
    ids = request_json(search_url).get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    summary_parameters = {
        "db": "pubmed", "retmode": "json", "id": ",".join(ids),
        "tool": "AwesomeBiomedicalAI-curator",
    }
    if email:
        summary_parameters["email"] = email
    summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urlencode(summary_parameters)
    result = request_json(summary_url).get("result", {})
    papers = []
    for pmid in result.get("uids", []):
        item = result.get(str(pmid), {})
        if not isinstance(item, dict):
            continue
        doi = ""
        for article_id in item.get("articleids", []) if isinstance(item.get("articleids"), list) else []:
            if isinstance(article_id, dict) and article_id.get("idtype") == "doi":
                doi = normalize_doi(str(article_id.get("value") or ""))
                break
        papers.append({
            "pmid": str(pmid),
            "doi": doi,
            "doi_url": f"https://doi.org/{doi}" if doi else "",
            "title": html.unescape(str(item.get("title") or "")).rstrip("."),
            "journal": html.unescape(str(item.get("fulljournalname") or item.get("source") or "")),
            "publication_date": str(item.get("pubdate") or ""),
            "date": "",
            "date_source": "PubMed publication summary; verify first-online month",
            "paper_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "metadata_source": summary_url,
            "discovery_sources": ["PubMed"],
        })
    return papers


def openalex_discover(rules: dict[str, Any], year: int, maximum: int) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    prefixes = tuple(str(prefix).casefold() for prefix in rules["allowed_journal_prefixes"])
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    per_query = min(maximum, 100)
    for query in rules["openalex_queries"]:
        parameters = {
            "search": query,
            "filter": f"from_publication_date:{year}-01-01,to_publication_date:{year}-12-31,type:article",
            "sort": "publication_date:desc",
            "per-page": str(per_query),
            "select": "id,doi,title,publication_date,primary_location",
        }
        if api_key:
            parameters["api_key"] = api_key
        url = "https://api.openalex.org/works?" + urlencode(parameters)
        payload = request_json(url)
        for item in payload.get("results", []):
            if not isinstance(item, dict):
                continue
            location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
            source = location.get("source") if isinstance(location.get("source"), dict) else {}
            journal = str(source.get("display_name") or "")
            if not journal.casefold().startswith(prefixes):
                continue
            title = html.unescape(str(item.get("title") or ""))
            signals = title_review_priority({"title": title}, rules)
            if signals["review_priority"] == "low":
                continue
            doi = normalize_doi(str(item.get("doi") or ""))
            publication_date = str(item.get("publication_date") or "")
            date = "".join(re.findall(r"\d+", publication_date)[:2])[:6]
            papers.append({
                "openalex_id": item.get("id") or "",
                "doi": doi,
                "doi_url": f"https://doi.org/{doi}" if doi else "",
                "title": title,
                "journal": journal,
                "publication_date": publication_date,
                "date": date,
                "date_source": "OpenAlex publication date; verify first-online month",
                "paper_url": location.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else item.get("id") or ""),
                "metadata_source": url,
                "discovery_sources": ["OpenAlex"],
            })
    return deduplicate_discovery(papers)


def deduplicate_discovery(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for paper in papers:
        key = normalize_doi(str(paper.get("doi") or "")) or normalize_identity(str(paper.get("title") or ""))
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(paper)
            merged[key].setdefault("discovery_sources", [])
            continue
        existing = merged[key]
        sources = list(dict.fromkeys([
            *existing.get("discovery_sources", []),
            *paper.get("discovery_sources", []),
        ]))
        existing["discovery_sources"] = sources
        for field in ("doi", "doi_url", "title", "journal", "publication_date", "date", "paper_url"):
            if not existing.get(field) and paper.get(field):
                existing[field] = paper[field]
    return list(merged.values())


def md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def evidence_text(record: dict[str, Any], field: str, errors: list[str]) -> str:
    value = record.get(field)
    if not isinstance(value, dict):
        errors.append(f"{field} must be an object with text and evidence")
        return ""
    text = str(value.get("text") or "").strip()
    if not text:
        errors.append(f"{field}.text is required")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{field}.evidence must contain at least one official source")
    else:
        for position, item in enumerate(evidence, start=1):
            if not isinstance(item, dict):
                errors.append(f"{field}.evidence[{position}] must be an object")
                continue
            if not str(item.get("url") or "").startswith("https://"):
                errors.append(f"{field}.evidence[{position}].url must use HTTPS")
            if not str(item.get("note") or "").strip():
                errors.append(f"{field}.evidence[{position}].note is required")
    return text


def validate_record(record: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if record.get("decision") != "include":
        errors.append("decision must be 'include' before rendering")
    date = str(record.get("date") or "")
    if not DATE_RE.fullmatch(date):
        errors.append("date must use YYYYMM")
    elif int(date[:4]) != int(rules["target_year"]):
        errors.append(f"date must be in the configured target year {rules['target_year']}")
    for field in ("model_name", "title", "paper_url", "doi", "venue", "model_type", "backbone"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            errors.append(f"{field} is required")
    if record.get("paper_url") and not str(record["paper_url"]).startswith("https://"):
        errors.append("paper_url must use HTTPS")
    if record.get("doi") and not normalize_doi(str(record["doi"])):
        errors.append("doi is not valid")
    venue = str(record.get("venue") or "")
    prefixes = tuple(str(prefix).casefold() for prefix in rules["allowed_journal_prefixes"])
    if venue and not venue.casefold().startswith(prefixes):
        errors.append("venue must begin with an allowed Nature-family prefix")

    model_size = record.get("model_size")
    evidence_text(record, "model_size", errors)
    if isinstance(model_size, dict) and model_size.get("status") not in MODEL_SIZE_STATUSES:
        errors.append("model_size.status is invalid")
    training_text = evidence_text(record, "training_adaptation", errors)
    if normalize_identity(training_text) in {"supervised", "selfsupervised"}:
        errors.append("training_adaptation must describe the concrete mechanism, not only a supervision label")
    evidence_text(record, "training_data", errors)
    evidence_text(record, "downstream_tasks", errors)

    modalities = record.get("modalities")
    if not isinstance(modalities, list) or not modalities or not all(
        isinstance(item, str) and item.strip() for item in modalities
    ):
        errors.append("modalities must be a non-empty list of strings")
    authors = record.get("authors", [])
    if not isinstance(authors, list) or not all(isinstance(item, str) and item.strip() for item in authors):
        errors.append("authors must be a list of non-empty strings")

    resources = record.get("resources", [])
    if not isinstance(resources, list):
        errors.append("resources must be a list")
    else:
        seen_urls: set[str] = set()
        for position, resource in enumerate(resources, start=1):
            if not isinstance(resource, dict):
                errors.append(f"resources[{position}] must be an object")
                continue
            label = str(resource.get("label") or "")
            url = str(resource.get("url") or "")
            if label not in RESOURCE_LABELS:
                errors.append(f"resources[{position}].label is not an allowed catalogue label")
            if not url.startswith("https://"):
                errors.append(f"resources[{position}].url must use HTTPS")
            if url in seen_urls:
                errors.append(f"resources[{position}].url duplicates another resource")
            seen_urls.add(url)

    performance = record.get("performance", [])
    if not isinstance(performance, list):
        errors.append("performance must be a list")
    else:
        for position, item in enumerate(performance, start=1):
            if not isinstance(item, dict):
                errors.append(f"performance[{position}] must be an object")
                continue
            for field in ("benchmark", "metric", "value"):
                if not str(item.get(field) or "").strip():
                    errors.append(f"performance[{position}].{field} is required")

    for field in ("supplement_url", "notebooklm_url"):
        value = record.get(field)
        if value not in (None, "") and not str(value).startswith("https://"):
            errors.append(f"{field} must be null or an HTTPS URL")
    return errors


def render_record(record: dict[str, Any], rules: dict[str, Any]) -> str:
    errors = validate_record(record, rules)
    if errors:
        raise CuratorError("Record validation failed:\n- " + "\n- ".join(errors))

    date = str(record["date"])
    display_date = f"{date[:4]}-{date[4:]}"
    model = str(record["model_name"])
    anchor = f"model-{slugify(model)}-{date}"
    doi = normalize_doi(str(record["doi"]))
    modalities = ", ".join(str(item) for item in record["modalities"])
    modality_codes = ", ".join(f"`{item}`" for item in record["modalities"])
    authors = " & ".join(str(item) for item in record.get("authors", []))

    overview = (
        f"| {date} | [{md_cell(model)}](#{anchor}) | {md_cell(record['venue'])} | "
        f"{md_cell(modalities)} | {md_cell(record['training_data']['text'])} | "
        f"{md_cell(record['model_size']['text'])} | {md_cell(record['training_adaptation']['text'])} | "
        f"{md_cell(record['downstream_tasks']['text'])} |"
    )
    citation_parts = [f"*{record['venue']}*", display_date]
    if authors:
        citation_parts.append(authors)
    citation_parts.append(f"[doi:{doi}](https://doi.org/{doi})")

    detail_lines = [
        f'<a id="{anchor}"></a>',
        "<details>",
        f"<summary><b>{html.escape(model)}</b> — {html.escape(str(record['title']))} <i>({html.escape(str(record['venue']))} {display_date})</i></summary>",
        "",
        f"**[{record['title']}]({record['paper_url']})**",
        "",
        " · ".join(citation_parts),
        "",
        "| | |",
        "| --- | --- |",
        f"| **Model** | {md_cell(model)} |",
        f"| **Model type** | {md_cell(record['model_type'])} |",
        f"| **Backbone** | {md_cell(record['backbone'])} |",
        f"| **Model size** | {md_cell(record['model_size']['text'])} |",
        f"| **Training / adaptation** | {md_cell(record['training_adaptation']['text'])} |",
        f"| **Training data** | {md_cell(record['training_data']['text'])} |",
        f"| **Downstream tasks** | {md_cell(record['downstream_tasks']['text'])} |",
        f"| **Modalities** | {modality_codes} |",
    ]
    for resource in record.get("resources", []):
        detail_lines.append(f"| **{resource['label']}** | [{resource['url']}]({resource['url']}) |")

    performance = record.get("performance", [])
    if performance:
        detail_lines.extend([
            "", "**Reported performance**", "",
            "| Benchmark | Metric | Value | Note |",
            "| --- | --- | --- | --- |",
        ])
        for item in performance:
            detail_lines.append(
                f"| {md_cell(item['benchmark'])} | {md_cell(item['metric'])} | "
                f"{md_cell(item['value'])} | {md_cell(item.get('note', ''))} |"
            )
    verification_note = str(record.get("verification_note") or "").strip()
    if verification_note:
        detail_lines.extend(["", f"> **Verification note:** {verification_note}"])
    detail_lines.extend(["", "</details>"])
    return "\n".join(["<!-- OVERVIEW ROW -->", overview, "", "<!-- DETAIL RECORD -->", *detail_lines, ""])


def check_one(value: str, repo_root: Path, model_name: str = "") -> dict[str, Any]:
    rules = load_rules()
    metadata = resolve_metadata(value)
    index = parse_catalog(repo_root / rules["target_page"])
    evaluation = evaluate_metadata(metadata, index, rules, model_name=model_name)
    return {
        "input": value,
        "metadata": metadata,
        "evaluation": evaluation,
        "record": record_skeleton(metadata, evaluation, model_name=model_name),
    }


def command_check(args: argparse.Namespace, repo_root: Path) -> None:
    result = check_one(args.input, repo_root, model_name=args.model_name or "")
    stem = candidate_stem(result["metadata"])
    destination = Path(args.output) if args.output else output_root(repo_root) / f"check-{stem}.json"
    write_json(destination, result)
    review_path = destination.with_suffix(".review.md")
    review_path.write_text(review_markdown(result["metadata"], result["evaluation"]), encoding="utf-8")
    print(json.dumps({
        "decision": result["evaluation"]["decision"],
        "json": str(destination),
        "review": str(review_path),
    }, ensure_ascii=False, indent=2))


def command_import_links(args: argparse.Namespace, repo_root: Path) -> None:
    source = Path(args.file)
    if not source.is_file():
        raise CuratorError(f"Link file does not exist: {source}")
    inputs = [
        line.strip() for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    results = []
    for value in inputs:
        try:
            results.append(check_one(value, repo_root))
        except CuratorError as exc:
            results.append({"input": value, "error": str(exc)})
    destination = Path(args.output) if args.output else output_root(repo_root) / f"import-{dt.date.today().isoformat()}.json"
    write_json(destination, {"count": len(results), "results": results})
    counts: dict[str, int] = {}
    for item in results:
        decision = item.get("evaluation", {}).get("decision", "error")
        counts[decision] = counts.get(decision, 0) + 1
    print(json.dumps({"counts": counts, "json": str(destination)}, ensure_ascii=False, indent=2))


def command_discover(args: argparse.Namespace, repo_root: Path) -> None:
    rules = load_rules()
    index = parse_catalog(repo_root / rules["target_page"])
    papers: list[dict[str, Any]] = []
    if args.source in {"pubmed", "all"}:
        papers.extend(pubmed_discover(rules, args.year, args.max))
    if args.source in {"openalex", "all"}:
        papers.extend(openalex_discover(rules, args.year, args.max))
    papers = deduplicate_discovery(papers)
    results = []
    for metadata in papers:
        results.append({"metadata": metadata, "evaluation": evaluate_metadata(metadata, index, rules)})
    priority_order = {"high": 0, "medium": 1, "low": 2, "other_page": 3}
    results.sort(key=lambda item: item["metadata"].get("publication_date", ""), reverse=True)
    results.sort(key=lambda item: priority_order[item["evaluation"]["review_priority"]])
    payload = {
        "sources": [args.source] if args.source != "all" else ["PubMed", "OpenAlex"],
        "pubmed_query": pubmed_query(rules, args.year) if args.source in {"pubmed", "all"} else None,
        "openalex_queries": rules["openalex_queries"] if args.source in {"openalex", "all"} else None,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "count": len(results),
        "results": results,
    }
    destination = Path(args.output) if args.output else output_root(repo_root) / f"discovery-{args.year}-{dt.date.today().isoformat()}.json"
    write_json(destination, payload)
    markdown_path = destination.with_suffix(".md")
    lines = [
        f"# Biomedical Images — Other discovery ({args.year})",
        "",
        "Discovery results require semantic scope review and primary-source verification.",
        "",
        "Start with `high`, then review `medium`. `low` and `other_page` remain visible to prevent silent false negatives.",
        "",
        "| Priority | Decision | Date | Journal | Title | DOI |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        metadata = item["metadata"]
        doi = metadata.get("doi") or ""
        doi_link = f"[DOI](https://doi.org/{doi})" if doi else "Not found"
        lines.append(
            f"| {item['evaluation']['review_priority']} | {item['evaluation']['decision']} | "
            f"{md_cell(metadata.get('publication_date', ''))} | "
            f"{md_cell(metadata.get('journal', ''))} | {md_cell(metadata.get('title', ''))} | {doi_link} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for item in results:
        decision = item["evaluation"]["decision"]
        counts[decision] = counts.get(decision, 0) + 1
    priorities: dict[str, int] = {}
    for item in results:
        priority = item["evaluation"]["review_priority"]
        priorities[priority] = priorities.get(priority, 0) + 1
    print(json.dumps({
        "counts": counts,
        "review_priorities": priorities,
        "json": str(destination),
        "markdown": str(markdown_path),
    }, ensure_ascii=False, indent=2))


def load_record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CuratorError(f"Could not read record JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CuratorError("Record JSON must contain one top-level object")
    return value


def command_validate(args: argparse.Namespace, _repo_root: Path) -> None:
    errors = validate_record(load_record(Path(args.record)), load_rules())
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


def command_render(args: argparse.Namespace, repo_root: Path) -> None:
    record = load_record(Path(args.record))
    markdown = render_record(record, load_rules())
    destination = Path(args.output) if args.output else output_root(repo_root) / f"candidate-{slugify(str(record.get('model_name') or 'paper'))}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(markdown, encoding="utf-8")
    print(json.dumps({"markdown": str(destination)}, ensure_ascii=False, indent=2))


def command_index(args: argparse.Namespace, repo_root: Path) -> None:
    rules = load_rules()
    index = parse_catalog(repo_root / rules["target_page"])
    destination = Path(args.output) if args.output else output_root(repo_root) / "catalog-index.json"
    write_json(destination, index)
    print(json.dumps({
        "counts": {key: len(value) for key, value in index.items()},
        "json": str(destination),
    }, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Biomedical Images — Other curation helper")
    parser.add_argument("--repo", type=Path, help="Repository root containing biomedical_images.md")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Resolve one URL/DOI and run deterministic gates")
    check.add_argument("input", help="Publisher URL or DOI")
    check.add_argument("--model-name", default="", help="Optional model name for duplicate checking")
    check.add_argument("--output", type=Path)
    check.set_defaults(handler=command_check)

    imported = subparsers.add_parser("import-links", help="Check one URL/DOI per line from a text file")
    imported.add_argument("file", type=Path)
    imported.add_argument("--output", type=Path)
    imported.set_defaults(handler=command_import_links)

    discover = subparsers.add_parser("discover", help="Discover candidates through PubMed and/or OpenAlex")
    discover.add_argument("--year", type=int, default=2026)
    discover.add_argument("--max", type=int, default=50, help="Maximum results per database query")
    discover.add_argument("--source", choices=("all", "pubmed", "openalex"), default="all")
    discover.add_argument("--output", type=Path)
    discover.set_defaults(handler=command_discover)

    validate = subparsers.add_parser("validate", help="Validate a completed paper record JSON")
    validate.add_argument("record", type=Path)
    validate.set_defaults(handler=command_validate)

    render = subparsers.add_parser("render", help="Validate and render a completed paper record JSON")
    render.add_argument("record", type=Path)
    render.add_argument("--output", type=Path)
    render.set_defaults(handler=command_render)

    index = subparsers.add_parser("index", help="Build a DOI/title/model index from the current catalogue")
    index.add_argument("--output", type=Path)
    index.set_defaults(handler=command_index)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        repo_root = find_repo_root(args.repo)
        args.handler(args, repo_root)
        return 0
    except CuratorError as exc:
        print(f"curator error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
