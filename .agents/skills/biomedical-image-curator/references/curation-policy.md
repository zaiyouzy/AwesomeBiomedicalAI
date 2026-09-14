# Biomedical Images — Other curation policy

Read this file before deciding whether a candidate belongs in `biomedical_images.md` or extracting its detailed record.

## Inclusion target

- First published online in 2026. Older existing records remain; the 2026 rule applies to new discovery.
- Published in a Nature Portfolio journal whose displayed journal name begins with `Nature`. The configured priority set is Nature, Nature Medicine, Nature Methods, Nature Biomedical Engineering, and Nature Communications.
- The primary data are biomedical images outside the repository's pathology and radiology pages.
- In-scope modalities include ultrasound; microscopy and fluorescence microscopy; retinal imaging and fundus photography; OCT; dermatology; endoscopy; cytology; and other clearly biomedical image modalities that do not belong elsewhere.
- The paper's central contribution is an important AI model, system, or image-analysis method. Prefer foundation/general-purpose models or methods with clear clinical or scientific impact, but do not require the authors to use the phrase “foundation model.”
- The paper is absent from the current catalogue after DOI, normalized title, and model-name checks.

## Exclusions

- Conferences such as MICCAI.
- IEEE and other sources outside the current Nature-journal target.
- arXiv or another preprint used in place of the published journal article.
- Work primarily about computational pathology, CT/MRI radiology, EHR/longitudinal health data, general multimodal systems, or LLMs.
- Papers that only use an ordinary AI method as a minor analysis tool rather than introducing or validating a material imaging method.

When page ownership is ambiguous, return `needs_review` and explain the competing pages. Do not force a binary decision.

Automated classification notes (used by `discover-v2` fixtures and gates):

- LLM/MLLM-primary work (for example MLLM-EDR) is treated as `other_page`
  because its primary contribution is the language/multimodal system.
- Cytology itself remains potentially in scope, but pathology-dominant
  whole-slide analysis is **not automatically accepted**; ambiguous cases
  stay `needs_review` and are flagged `other_page` so a human decides.
- Every regression fixture in `tests/fixtures/` records the reason for its
  expected classification.

## Required record content

- First-online month as `YYYYMM`.
- Model name and paper link.
- Journal.
- Imaging modality.
- Training data with the most informative patient/image/video/slide/dataset counts available.
- Model size with provenance: reported, computed from official weights, computed from official code/configuration, not reported, or not publicly verifiable.
- Training/adaptation: objective, initialization, frozen/trainable components, and LoRA/full fine-tuning/linear probing or other concrete mechanism.
- Specific downstream diagnostic, segmentation, generation, prediction, restoration, or clinical tasks.
- Official Code, Weights, Data, and Project page/demo links when they exist. Labels must describe the destination accurately.
- Representative reported performance when a compact benchmark/metric/value table is informative.

## Evidence hierarchy

1. Article version of record.
2. Publisher supplementary material.
3. Official author-maintained code repository and released configuration.
4. Official model weights or formal data/software record such as Zenodo.
5. Official project page, used only for claims it directly supports.

Search results, Scholar Labs descriptions, third-party summaries, lab homepages, and inferred architecture defaults are discovery aids, not detailed-field evidence.

First-online verification hierarchy (deterministic stage, `discover-v2`):

1. Official Nature article page citation metadata.
2. Crossref `published-online` metadata.
3. PubMed electronic publication metadata.
4. Unresolved → the candidate is sent for manual verification.

Store the provenance per candidate. If trustworthy sources conflict, flag the
candidate for review instead of silently choosing a date. Papers whose true
first-online year is 2025 but whose issue year is 2026 are excluded as
`before_target`; these traps are pinned as regression fixtures in
`tests/fixtures/date-traps.json`.

## Model-size decisions

- `reported`: the article or supplement explicitly states the count.
- `computed_weights`: count obtained from official released weight metadata.
- `computed_code`: reproducible count from the official architecture and exact published configuration.
- `not_reported`: reviewed materials do not state a count, but public artifacts may still exist.
- `not_publicly_verifiable`: neither the reviewed publication nor adequate official public artifacts support a count.

Never substitute the parameter count of a vaguely similar backbone. For multi-component systems, report components separately and do not sum models that are not executed as one model.

## PDF and NotebookLM handling

Do not add PDFs to this repository. If a maintainer later creates a NotebookLM, include the article and supplement in the private notebook and use names such as:

- `202605-Nat-Model.pdf`
- `202605-Nat-Model-supp.pdf`

The public catalogue should contain only the NotebookLM share link approved by the maintainer.
