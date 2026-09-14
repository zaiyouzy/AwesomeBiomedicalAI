# Deterministic discovery v2 — maintainer runbook

This file explains the deterministic discovery stage (`discover-v2`) for a
non-technical maintainer: what it does, what it produces, how to act on the
queue, and how the reviewed-papers ledger works.

## What the weekly Action does

Every Monday (and on demand from the Actions page) the read-only workflow:

1. Runs the Python unit tests.
2. Searches **PubMed in two complementary passes** over a **rolling 21-day
   window** (with a 3-day overlap so nothing falls between runs):
   - **Pass 1** — papers whose title/abstract contains *an AI term* **and**
     *a biomedical-imaging modality term* (ultrasound, microscopy,
     light-sheet, retina/fundus, OCT, dermatology, endoscopy, cytology,
     spatial proteomics, ophthalmic/surgical video, virtual staining, ...).
   - **Pass 2** — papers whose **title alone** contains a modality term,
     even without any AI vocabulary (catches papers like MouseMapper whose
     AI wording is hyphenated or unusual).
3. Fetches PubMed **abstracts** for every candidate and re-scores the
   priority signals from title + abstract.
4. Optionally adds an **OpenAlex** sweep of the same journals (source-scoped
   by ISSN, paginated, rate-limit tolerant). If OpenAlex fails, the run
   continues and records the reason.
5. Removes cross-source duplicates and compares every candidate with
   `biomedical_images.md` (DOI/title/model duplicate checks).
6. Verifies the **first-online date** for every high/medium candidate using
   this evidence order:
   1. official Nature article page metadata
   2. Crossref `published-online`
   3. PubMed electronic publication date
   4. unresolved → sent to manual review

   Provenance is stored per candidate. If trusted sources disagree the
   candidate is flagged `conflict` instead of silently picking one date.
   Papers with a 2025 first-online date but a 2026 issue year are excluded
   automatically (`before_target`).
7. Suppresses candidates already decided in `reviewed-papers.json`.
8. Writes a **compact review queue** (default max 10 new high/medium
   candidates) plus a **complete raw audit** and a human-readable summary.

## Outputs (all under `.curator/`, uploaded as an artifact for 30 days)

| File | Contents |
| --- | --- |
| `weekly-review-queue.json` | Machine-readable compact queue (≤10 by default), one object per candidate with DOI, title, journal, first-online month + provenance, priority, retrieval pass, abstract snippet, reasons. Sized so a future AI screening stage can read it cheaply. |
| `weekly-review-queue.md` | The same queue as readable Markdown. |
| `weekly-raw-audit.json` | **Every** raw candidate (including duplicates, exclusions, low priority and other-page items) with full metadata, scope signals, gate reasons, date provenance and source errors — the audit trail. |
| `weekly-summary.md` | Counts, source status, queue list, file names, and instructions (shown in the run summary page). |

Nothing is ever pushed to `main`: this workflow has `contents: read` only.

## How to act on a queue

1. Open the Actions run and download the `biomedical-image-candidates-N`
   artifact, or read `weekly-review-queue.md` directly.
2. For each queued entry, verify the paper against
   `references/curation-policy.md` (scope, evidence rules, model size,
   training/adaptation, resources). Every queue entry is only a *suggestion*.
3. Decide:
   - **accept** → add the paper to `biomedical_images.md` in the usual
     human-reviewed PR and record the DOI in the ledger as `accepted`.
   - **reject / other page** → record the DOI in the ledger as `excluded` or
     `other_page` with a one-line reason.
4. Merging that PR makes the decision permanent; the next weekly run will
   automatically skip those DOIs.

## The reviewed-papers ledger

File: `.agents/skills/biomedical-image-curator/assets/reviewed-papers.json`

This tracked, human-reviewed file is the only persistent state. The
automation **reads** it and never writes it. Schema:

```json
{
  "schema": "biomedical-images-other.reviewed-papers",
  "version": 1,
  "updated": "YYYY-MM-DD",
  "entries": [
    {
      "identifier": "10.1038/s41467-026-xxxxx-x",
      "identifier_type": "doi",
      "status": "accepted",
      "reviewed_at": "YYYY-MM-DD",
      "reason": "Optional one-line note"
    }
  ]
}
```

- `identifier_type`: `doi` (preferred, also accepts Nature article URLs),
  `pmid`, `url`, or `other`.
- `status`: `accepted` (added to the catalogue), `excluded` (reviewed and
  rejected), or `other_page` (belongs to a different catalogue page).
- Only *human decisions* belong here. Never paste automated raw candidates
  into this file; they live in the audit artifact.

Validate the file locally before merging a ledger PR:

```text
python .agents/skills/biomedical-image-curator/scripts/curator.py \
  --repo . validate-ledger
```

## Known behaviour to expect

- **Repeat items:** the 3-day window overlap can show an *unreviewed*
  candidate for two consecutive weekly queues. Once a human records a
  decision in the ledger, it never reappears.
- **Empty weeks are fine:** if the queue is empty the run still uploads the
  raw audit so nothing is silently lost.
- **Sources are optional:** a PubMed-only run (`--source pubmed`) or an
  OpenAlex outage never breaks the run; check `weekly-summary.md` → “Source
  status” for any provider errors.

## Running it locally

```text
python .agents/skills/biomedical-image-curator/scripts/curator.py \
  --repo . discover-v2 \
  --source all \
  --queue-limit 10 \
  --prefix .curator/weekly
```

Benchmark mode over the whole target year (used in the retrospective recall
test):

```text
python .agents/skills/biomedical-image-curator/scripts/curator.py \
  --repo . discover-v2 \
  --source pubmed \
  --full-year \
  --queue-limit 50 \
  --prefix .curator/benchmark
```

See also `scripts/curator.py` (`discover-v2 --help`) for all flags.
