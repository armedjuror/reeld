---
name: reeld-voice-profile
description: Use when Ajwad wants to set up or refine his voice and tone for reeld reels ("my voice", "tone", "how I sound", "voice profile", "update my style"), or when check_voice_profile shows sections missing. Interviews him until the tone is unambiguous, tests it on sample lines, and saves it to reeld through the MCP.
---

# reeld voice profile interview

## Goal

A voice profile specific enough that a stranger could write a reel line Ajwad would read without changing a word. Every script, critic and visuals step in reeld is fed this profile, so anything vague here becomes generic reels later. Keep asking until every section is clear and he confirms test lines sound like him. There is no question limit.

## Tools (reeld MCP)

| Tool | Use |
| --- | --- |
| `check_voice_profile` | First call. Shows each section as ok / thin / missing, with the open questions. |
| `get_voice_template` | Section list, purpose, and what each must pin down. |
| `get_user_profile` | Read the full saved profile. |
| `update_voice_section` | Save one confirmed section (replaces only that section). |

If the reeld tools aren't available, say so, run the interview anyway, and at the end give him the finished profile to paste into reeld → Settings → Your voice.

## How to interview

1. **Start from what exists.** Call `check_voice_profile`. If he's refining, only work on thin or missing sections, plus anything he names.
2. **Ask for evidence before opinions.** Early on, ask for 2–3 real scripts, captions or transcripts he's happy with (pasted text, or a reel he liked of his own). Derive observations from them ("you open with a question 3 of 3 times, use 'ningal', keep English for technical words"), then confirm each one with him. His real lines beat his self-description.
3. **One question at a time**, short, in plain language. Where a question has a scale or a choice, offer 2–4 concrete options with an example line for each, plus "something else". He's fast and terse: accept short answers, and follow up only where the answer is still ambiguous.
4. **Calibrate with A/B lines.** For tone, pacing, humour and hooks, write the same line 2–3 ways (in his language mix) and ask which sounds like him, or what to change. Repeat until his pick is consistent. Use his series topics (math, software, productivity, food, elections) for the examples.
5. **Pin numbers and words down.** "Casual" isn't an answer; "4/5 casual, says 'machane' only in hooks" is. Get sentence length, code-mixing ratio, words he uses, words he never uses.
6. **Save as you go.** When a section is clear, show it to him in 3–8 lines (his wording, one example line where useful), and on his OK call `update_voice_section`. Don't save anything he hasn't confirmed, and never invent facts about him.
7. **Track progress** out loud in one line now and then ("8/13 sections clear; next: humour").

## Sections (in this order, skipping ones already clear)

Who I am on camera · Audience · Language · Tone · Pacing and sentences · Humour · Explaining · Hooks · Signature phrases · Call to action · Visual brand · Never · Sample lines.

`get_voice_template` lists the exact questions per section. Language matters most for him: settle the speaking language and mix (e.g. Malayalam with English technical terms), which words stay English, how the script file is written (Malayalam script, transliteration, or English that he translates), and caption language.

## Final check (don't skip)

1. Call `check_voice_profile`; every section should be `ok`.
2. Write a 4–6 line test script in his voice on a topic he picks, plus one deliberately off-voice version without saying which is which. Ask which one is his and what's still off. Fix the profile sections that caused any miss and re-test until he says the right one sounds like him without edits.
3. Save any last changes, then tell him in one line: profile version, sections clear, and that every new script will use it.

## Series-specific tone

If he wants one series to sound different (e.g. Ballot Math more neutral, Varthaai more playful), don't change the global profile. Put the override in that series' bible under the same section heading (`get_series` → edit bible → `validate_series_package` → `update_series`), since the bible overrides the profile section by section.
