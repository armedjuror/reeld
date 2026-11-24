"""The voice profile: sections, what each must pin down, and completeness checks.

Every script, critic and visuals step is fed this profile (merged with the
series bible), so vague entries produce generic reels. Each section lists the
questions an interview must answer before the section counts as clear.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .context import merge_sections, split_sections


@dataclass(frozen=True)
class Section:
    heading: str
    purpose: str
    must_answer: tuple[str, ...]


SECTIONS: tuple[Section, ...] = (
    Section("Who I am on camera", "The persona viewers meet.", (
        "Name or handle you use, and how you introduce yourself (or don't)",
        "The role you play for viewers: teacher, friend, insider, skeptic, storyteller",
        "What viewers should feel about you after one reel",
    )),
    Section("Audience", "Who the default listener is.", (
        "Who they are (age, background, what they already know)",
        "What they care about and what bores them",
        "How you address them: you / ningal / nee / team / no direct address",
    )),
    Section("Language", "Which languages, how mixed, how written.", (
        "Spoken language(s) and the code-mixing ratio (e.g. Malayalam with English technical words)",
        "Which words stay in English, which get translated",
        "Script for the script file: Malayalam script, transliteration, or English to translate yourself",
        "Caption language and style",
    )),
    Section("Tone", "Where you sit on each dial, with an example line.", (
        "Formal ↔ casual (1–5)",
        "Calm ↔ high-energy (1–5)",
        "Earnest ↔ playful (1–5)",
        "Neutral ↔ opinionated (1–5), and topics where you never take sides",
        "How you sound when you disagree or debunk something",
    )),
    Section("Pacing and sentences", "Rhythm of the voiceover.", (
        "Typical sentence length (words) and the longest acceptable",
        "Pauses, rhetorical questions, repetition: when and how often",
        "Words per second you speak at (or a sample timing)",
    )),
    Section("Humour", "If and how you are funny.", (
        "Kind of humour (dry, self-deprecating, puns, local references) or none",
        "Where it goes (hook, mid, sign-off) and what's off-limits",
    )),
    Section("Explaining", "How ideas are made intuitive.", (
        "Preferred devices: analogies, running example, stories, numbers, visual metaphors",
        "Go-to sources of examples (Kerala life, cricket, cooking, office life…)",
        "How much maths/jargon is acceptable before you must simplify",
    )),
    Section("Hooks", "The first 3 seconds.", (
        "Hook styles that sound like you, with 2–3 examples in your words",
        "Hook styles you hate (clickbait patterns, shouting, fake urgency)",
    )),
    Section("Signature phrases", "Words that make it yours.", (
        "Phrases you naturally use (openers, transitions, sign-offs)",
        "Words and phrases to never use",
    )),
    Section("Call to action", "How a reel ends.", (
        "Default CTA wording pattern (e.g. comment keyword for a DM) and tone",
        "Sign-off line, if any",
    )),
    Section("Visual brand", "Defaults for Claude-made visuals.", (
        "Palette (hex), fonts, mascot or recurring character and how it behaves",
        "On-screen text density and caption placement",
        "Visual styles to avoid",
    )),
    Section("Never", "Hard rules.", (
        "Claims, topics, or styles that must never appear",
    )),
    Section("Sample lines", "Calibration: real lines in your voice.", (
        "3–6 lines you have actually said or written that sound exactly like you",
        "1–2 lines that sound wrong for you, and why",
    )),
)


def template() -> str:
    """Empty profile skeleton with the questions as comments."""
    out = ["# Voice profile\n"]
    for sec in SECTIONS:
        qs = "\n".join(f"<!-- {q} -->" for q in sec.must_answer)
        out.append(f"## {sec.heading}\n\n<!-- {sec.purpose} -->\n{qs}\n")
    return "\n".join(out)


def coverage(profile_md: str) -> dict:
    """Which sections are filled. A section is 'thin' under ~15 words of real content."""
    _, secs = split_sections(profile_md or "")
    have = {h.strip().lower(): re.sub(r"<!--.*?-->", "", b, flags=re.DOTALL).strip() for h, b in secs}
    result = []
    for sec in SECTIONS:
        body = have.get(sec.heading.lower(), "")
        words = len(body.split())
        status = "missing" if not body else ("thin" if words < 15 else "ok")
        result.append({"section": sec.heading, "status": status, "words": words,
                       "must_answer": list(sec.must_answer) if status != "ok" else []})
    extra = [h for h, _ in secs if h.strip().lower() not in {s.heading.lower() for s in SECTIONS}]
    done = sum(1 for r in result if r["status"] == "ok")
    return {"complete": done == len(SECTIONS), "ok": done, "total": len(SECTIONS),
            "sections": result, "extra_sections": extra}


def update_section(profile_md: str, heading: str, body: str) -> str:
    """Replace (or add) one `## heading` section, leaving the rest untouched."""
    return merge_sections(profile_md or "", {heading.strip(): body.strip()})
