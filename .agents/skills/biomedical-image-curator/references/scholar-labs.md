# Scholar Labs-assisted discovery

Use this mode when the user asks to use Google Scholar Labs, provides Scholar Labs results, or ordinary database discovery appears to miss semantically related work.

## Browser-assisted mode

1. Use the available browser-control capability and the user's existing signed-in session. Never request credentials or inspect authentication storage.
2. Ask a scoped English research question covering the configured year, Nature-journal constraint, Biomedical Images — Other modalities, and page exclusions.
3. Use follow-up questions to narrow false positives, especially pathology, CT/MRI radiology, conferences, and preprints.
4. Collect stable paper landing-page URLs or DOIs for plausible candidates. Do not copy Scholar Labs' generated explanation into the catalogue.
5. Save one URL or DOI per line and run `curator.py import-links`.
6. Continue with primary-source evidence review for every surviving paper.

A useful initial query is:

```text
Find AI models, systems, or important image-analysis methods first published online in 2026 in Nature Portfolio journals whose names begin with “Nature”. Focus on ultrasound, microscopy, fluorescence microscopy, retinal or fundus imaging, OCT, dermatology, endoscopy, cytology, and other biomedical imaging outside pathology and CT/MRI radiology. Exclude conference papers, IEEE papers, arXiv-only preprints, pathology, CT/MRI radiology, EHR, and LLM-focused work. Prefer foundation or general-purpose models and methods with clear clinical or scientific impact.
```

## Fallback mode

If browser control or Scholar Labs access is unavailable, ask the user to paste the candidate URLs or DOIs. Do not attempt unattended scraping or build the MVP around Scholar Labs' interface.

## Interpretation boundary

Scholar Labs is a high-value semantic discovery surface, not the catalogue's evidence store. A candidate still must pass year/journal checks, DOI/title/model deduplication, scope review, and detailed verification from the article, supplement, official code, official weights, and formal records.
