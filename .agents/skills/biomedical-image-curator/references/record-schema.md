# Paper record schema

The `render` command consumes one UTF-8 JSON object. Start from `assets/paper-record.template.json` and preserve the value types below.

## Decision and identity

- `decision`: must be `include` before rendering.
- `date`: first-online month, exactly six digits `YYYYMM`.
- `model_name`, `title`, `paper_url`, `venue`, `model_type`, `backbone`: non-empty strings.
- `doi`: normalized DOI string without a `https://doi.org/` prefix.
- `authors`: optional list of display-name strings.
- `modalities`: non-empty list of modality strings.

## Evidence-backed fields

`model_size`, `training_data`, `training_adaptation`, and `downstream_tasks` are objects:

```json
{
  "text": "Informative catalogue-ready statement",
  "evidence": [
    {
      "url": "https://official-source.example/...",
      "location": "Methods, Model architecture",
      "note": "What this source supports"
    }
  ]
}
```

Each object requires non-empty `text` and at least one evidence item. Evidence notes should be short paraphrases, not long quotations.

`model_size` also requires `status`, one of:

- `reported`
- `computed_weights`
- `computed_code`
- `not_reported`
- `not_publicly_verifiable`

The rendered Markdown does not expose the internal evidence list; it is retained in JSON for review.

## Resources

`resources` is an optional list of `{ "label", "url" }` objects. Allowed labels follow the current catalogue, including `Code`, `Weights`, `Data`, `Project page / demo`, `Code / models`, `Code / weights`, `Implementation`, `Reproducibility code`, `Original research code`, `Interactive tool`, `Interactive data`, and `Weights / example data`.

Every resource URL must use HTTPS and its label must describe the destination. Supplementary material belongs in evidence or `supplement_url`; it is not committed to GitHub.

## Performance and notes

`performance` is an optional list of objects with non-empty `benchmark`, `metric`, `value`, and optional `note`.

`verification_note` is an optional concise public note explaining computed values, component boundaries, unavailable artifacts, or other interpretation needed by readers.

`notebooklm_url` may be null. The MVP does not create or populate NotebookLM.

## Output

The renderer creates one fragment containing:

1. A single overview-table row matching the current columns in `biomedical_images.md`.
2. An anchor and expandable `<details>` record.
3. A performance table when provided.
4. A verification note when provided.

The fragment is a proposal. Inserting it into the correct year section and updating the paper count/last-updated label remain explicit catalogue-edit actions.
