---
name: biomedical-image-curator
description: Curate or discover 2026 Nature-family AI papers for biomedical_images.md, including scope screening, DOI/title/model deduplication, evidence-backed metadata extraction, and catalogue-format Markdown drafts. Use for Biomedical Images — Other; do not route pathology, CT/MRI radiology, EHR, or LLM catalogue work here.
---

# Biomedical Image Curator

Maintain the repository's `biomedical_images.md` without weakening its current evidence standard. Treat that file as the output source of truth.

## Modes

- **Check one paper:** resolve a paper URL or DOI, pre-screen year/journal, and check duplicates.
- **Check a link batch:** accept one URL/DOI per line, including links copied from Scholar Labs.
- **Discover candidates:** query PubMed plus a lightweight OpenAlex freshness layer for the configured 2026 Nature-journal scope, then semantically review the returned candidates.
- **Render a record:** validate a completed paper JSON record and generate an overview row plus expandable detail card.

Use the deterministic helper from the repository root:

```text
python .agents/skills/biomedical-image-curator/scripts/curator.py --help
```

The helper uses only the Python standard library. It writes draft artifacts under `.curator/`, which is ignored by Git. It does not edit `biomedical_images.md`, commit, push, open a pull request, upload PDFs, or access NotebookLM.

## Shared workflow

1. Locate the repository root containing `biomedical_images.md`.
2. For a paper link or DOI, run `check`. For Scholar Labs results, save one link per line and run `import-links`. For database discovery, run `discover`.
   In discovery output, review `high` first and then `medium`. `low` and `other_page` are advisory queues, not automatic exclusions.
3. Stop immediately on `duplicate` or deterministic `exclude` unless the user asks for an audit.
4. For `needs_review`, read the paper, supplementary material, official code, official weights, and formal data records as available.
5. Fill a paper record that follows [references/record-schema.md](references/record-schema.md). Do not use search snippets as evidence for detailed fields.
6. Run `render`; correct every validation error rather than weakening the validator.
7. Compare the generated fragment with the current `biomedical_images.md`. Present it as a draft unless the user explicitly asks to apply it.

Before semantic inclusion/exclusion or evidence extraction, read [references/curation-policy.md](references/curation-policy.md). Read [references/scholar-labs.md](references/scholar-labs.md) only when Scholar Labs is requested or would materially improve discovery.

## Non-negotiable evidence rules

- Use the first online publication month as `YYYYMM` without a hyphen.
- Never infer model size. Use a reported count, a count from official weights, or a reproducible count from official code/configuration. Otherwise use `Not reported` or `Not publicly verifiable`.
- Describe the actual training/adaptation mechanism; `supervised` or `self-supervised` alone is insufficient.
- Link labels must match their destinations. A lab homepage is not model code; a demo is not weights.
- Publisher PDFs and supplementary PDFs must never be committed to this public repository.
- Scholar Labs, PubMed, and OpenAlex are discovery sources. They are not sufficient evidence for model size, training procedure, or performance.

## Scholar Labs boundary

Scholar Labs may be used through an available logged-in browser to discover candidate papers and follow related questions. Collect stable paper URLs or DOIs, then pass them through the same deterministic and evidence-review workflow. Do not scrape Scholar Labs, depend on its UI for unattended automation, or treat its generated descriptions as verified facts.

## GitHub boundary

This MVP produces drafts only. Do not modify the catalogue, create commits, push branches, or open pull requests unless the user separately authorizes that action. A future scheduled workflow must reuse the same rules and validator and should propose a draft pull request rather than write directly to `main`.
