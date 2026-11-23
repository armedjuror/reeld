---
name: reeld-series-package
description: Use when Ajwad wants to design a new reel series (or redesign one) for reeld. Interviews him, drafts every part of the series package, validates it, and saves it to reeld through the reeld MCP. Trigger words - new series, series package, push series to reeld.
---

# reeld series package generator

## Goal

Produce one complete series package that reeld can run without further chat: every record below, filled, validated and saved to the database through the reeld MCP. Never write local files as the output.

## Tools (reeld MCP)

| Tool | Use |
| --- | --- |
| `get_user_profile` | Read the voice profile. Always first. |
| `set_user_profile` | Only when he asks to change his profile. |
| `list_series` / `get_series` | Find an existing series and its current package. |
| `validate_series_package` | Check a draft. Fix every `error`; weigh each `warning`. |
| `create_series` | Save a new series, after his approval. |
| `update_series` | Save changes to an existing series, after his approval. |

If the reeld tools are not available, say so and stop; do not save the package anywhere else.

## Before starting

1. Call `get_user_profile`. The bible records only sections that differ from it; never copy profile content into the bible.
2. If the series may already exist, call `list_series` and `get_series`, and work as an edit of the returned package.

## Step 1: Interview (one question at a time, write nothing yet)

Cover: purpose and audience; platform and format (face or voiceover, language, captions); length and cadence; tone or visual overrides versus the profile; CTA pattern and comment-keyword deliverable; per-episode automations (blog, PDF, other); arc of the series and rough episode count; launch date and whether dates are precise or vague.

## Step 2: Research (only if the series needs it)

A concise, cited brief that grounds the plan. It stays out of the package except as the source for the plan lines.

## Step 3: Draft the package

The package is one JSON object (the tools publish its full schema):

| Field | Rules |
| --- | --- |
| `slug`, `name` | Slug is lowercase-hyphenated, e.g. `ballot-math`. |
| `bible` | Markdown with `## Section` headings (Audience, Purpose, Format, Length, Language, CTA, plus overrides). Under one page. A section with the same heading as a profile section replaces it. |
| `plan` | One entry per episode: `number`, `title`, `core_idea`, `hook`, `keyword` (one word, unique), `slot`, optional `overrides` (`{"Length": "90 s"}`). |
| `slot` | `{"kind": "precise", "date": "YYYY-MM-DD", "time": "HH:MM"}`; `week`/`month` with any `date` inside it; `{"kind": "after", "after_episode": N}`; or `unscheduled`. |
| `skills.script` | How to research and write one episode. Refers to the bible and plan line; never restates them. |
| `skills.rubric` | 6–12 checkable pass/fail criteria as a bullet list. No vague words like "engaging". |
| `skills.visuals` | Scene and illustration rules for this series; reference shared brand skills by name. |
| `skills.auto-<name>` | One per automation: input, output format, destination. |
| `automations` | `[{"name": "blog", "order": 1, "config": {...}}]`; each needs a matching `auto-<name>` skill. |
| `continuity` | Empty for a new series (the pipeline fills it with `## Ep N` entries). |
| `learnings` | Empty, or only rules he stated. |

Token rules: no duplication across records; each record is self-contained for the stage that loads it; nothing that belongs to the user profile.

## Step 4: Validate

Call `validate_series_package`. Fix every error and redraft until it returns `ok: true`. Address warnings or tell him why one stands.

## Step 5: Review and save

Show him a short summary: episode count, cadence, automations, the first three plan lines, and any remaining warnings. Only after he approves, call `create_series` (or `update_series` for an existing series) and report the result, including any `kept_in_progress` episodes. Never save without his approval.
