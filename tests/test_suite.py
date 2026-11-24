import asyncio
import json

import pytest

from conftest import sample_package
from suite import documents
from suite.context import continuity_window, merge_sections, resolve
from suite.models import DocKind, EpisodeState, ScopeType, SlotKind, VersionSource
from suite.package import PackageError, export_package, get_series, save_package, validate_package
from suite.users import current_user_id

# ─── Merging ──────────────────────────────────────────────────────────────────


def test_merge_sections_overrides_in_place_and_appends():
    voice = "I narrate casually.\n\n## Tone\n\nWarm.\n\n## Length\n\n45 s\n"
    bible = "## length\n\n60 s\n\n## Audience\n\nStudents\n"
    merged = merge_sections(voice, bible, {"Length": "90 s"})
    assert merged.index("## Tone") < merged.index("## Length") < merged.index("## Audience")
    assert "90 s" in merged and "60 s" not in merged and "45 s" not in merged
    assert merged.startswith("I narrate casually.")
    assert merged.count("## Length") == 1


def test_continuity_window_only_earlier_and_capped():
    md = "\n\n".join(f"## Ep {n}\n\ncovered {n}" for n in range(1, 9))
    out = continuity_window(md, before_episode=8, n=3)
    assert "covered 5" in out and "covered 7" in out
    assert "covered 4" not in out and "covered 8" not in out


# ─── Validation ───────────────────────────────────────────────────────────────


def test_valid_package_has_no_errors():
    issues = validate_package(sample_package())
    assert [i for i in issues if i.severity == "error"] == []


def test_validation_catches_real_problems():
    pkg = sample_package()
    pkg.plan[1].keyword = "fptp"  # duplicate, case-insensitive
    pkg.plan[2].slot.after_episode = 5  # not an earlier episode
    pkg.skills.pop("auto-blog")  # automation without a skill
    pkg.skills["rubric"] = "- Make it engaging\n- Good hook"
    errors = {(i.where, i.message.split(" ")[0]) for i in validate_package(pkg) if i.severity == "error"}
    wheres = {w for w, _ in errors}
    assert {"plan[2]", "plan[3]", "automations.blog"} <= wheres
    warnings = [i for i in validate_package(pkg) if i.severity == "warning"]
    assert any("vague" in w.message for w in warnings)
    assert any("criteria found" in w.message for w in warnings)


# ─── Import / update ──────────────────────────────────────────────────────────


def test_create_then_update_versions_only_changed_docs(db):
    with db.session_scope() as s:
        uid = current_user_id(s)
        res = save_package(s, uid, sample_package(), mode="create")
        assert res["episodes"]["created"] == [1, 2, 3]

    with db.session_scope() as s:
        uid = current_user_id(s)
        with pytest.raises(PackageError):
            save_package(s, uid, sample_package(), mode="create")

    pkg = sample_package()
    pkg.skills["script"] = "Write in Malayalam. One running example. No English idioms."
    pkg.plan = pkg.plan[:2]  # drop planned Ep 3
    pkg.plan[0].keyword, pkg.plan[1].keyword = "DYNASTY", "FPTP"  # swap keywords
    with db.session_scope() as s:
        uid = current_user_id(s)
        res = save_package(s, uid, pkg, mode="update")
        assert res["documents_changed"] == ["skill:script v2"]
        assert res["episodes"]["removed"] == [3]
        series = get_series(s, uid, "ballot-math")
        assert [e.keyword for e in series.episodes] == ["DYNASTY", "FPTP"]


def test_update_keeps_in_progress_episodes(db):
    with db.session_scope() as s:
        uid = current_user_id(s)
        save_package(s, uid, sample_package(), mode="create")
        series = get_series(s, uid, "ballot-math")
        series.episodes[2].state = EpisodeState.AWAITING_RECORDING
    pkg = sample_package()
    pkg.plan = pkg.plan[:2]
    with db.session_scope() as s:
        res = save_package(s, current_user_id(s), pkg, mode="update")
        assert res["kept_in_progress"] == [3]


def test_export_round_trips(db):
    with db.session_scope() as s:
        uid = current_user_id(s)
        save_package(s, uid, sample_package(), mode="create")
        exported = export_package(s, get_series(s, uid, "ballot-math"))
    original = sample_package()
    assert exported.plan == original.plan
    assert exported.skills == {k: v.strip() + "\n" for k, v in original.skills.items()}
    assert exported.automations == original.automations


# ─── Context resolver ─────────────────────────────────────────────────────────


def _seed(s):
    uid = current_user_id(s)
    documents.write(
        s,
        user_id=uid,
        scope_type=ScopeType.USER,
        scope_id=uid,
        kind=DocKind.VOICE,
        content="## Tone\n\nHonest, warm.\n\n## Length\n\n45 s\n",
        source=VersionSource.USER,
    )
    save_package(
        s,
        uid,
        sample_package(
            continuity="## Ep 1\n\nCovered spoilers.\n\n## Ep 2\n\nCovered dynasties.\n",
            learnings="- Open with a number",
        ),
        mode="create",
    )
    return get_series(s, uid, "ballot-math")


def test_script_context_layers_and_scoping(db, tmp_path):
    with db.session_scope() as s:
        series = _seed(s)
        ep3 = series.episodes[2]
        ctx = resolve(s, ep3, "script")
        body = ctx.render()
        profile = next(p for p in ctx.parts if p.name == "profile").text
        assert "Honest, warm." in profile  # from voice
        assert "General Malayali audience" in profile  # from bible
        assert "90 s" in profile and "60–75 s" not in profile and "45 s" not in profile
        assert "Covered spoilers." in body and "Covered dynasties." in body
        assert "Open with a number" in body
        assert "Ep 2 (previous)" in body
        assert ctx.skill and ctx.skill.name == "skill:script"
        assert "rubric" not in body.lower()  # critic material stays out
        manifest = ctx.manifest()
        assert manifest["tokens_estimate"] > 0
        json.dumps(manifest)  # storable on StageRun

        paths = ctx.materialise(tmp_path / "work")
        assert paths["context"].read_text().startswith("# Context: ballot-math Ep 3")
        assert "Malayalam" in paths["skill"].read_text()


def test_critic_uses_draft_and_rubric_only(db):
    with db.session_scope() as s:
        series = _seed(s)
        ctx = resolve(s, series.episodes[0], "critic", draft="DRAFT TEXT")
        names = [p.name for p in ctx.parts]
        assert "continuity" not in names and "neighbours" not in names
        assert "DRAFT TEXT" in ctx.render()
        assert ctx.skill.name == "skill:rubric"


def test_automation_context_reports_missing_inputs(db):
    with db.session_scope() as s:
        series = _seed(s)
        ctx = resolve(s, series.episodes[0], "automation", automation="blog")
        assert ctx.skill.name == "skill:auto-blog"
        assert set(ctx.missing) == {"research", "script"}


# ─── MCP server ───────────────────────────────────────────────────────────────


def _call(name, args):
    from suite.mcp_server import server

    result = asyncio.run(server.call_tool(name, args))
    payload = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if payload is None:
        payload = json.loads(result.content[0].text)
    return payload.get("result", payload) if isinstance(payload, dict) and set(payload) == {"result"} else payload


def test_mcp_round_trip(db):
    pkg = sample_package().model_dump(mode="json")
    assert _call("validate_series_package", {"package": pkg})["ok"] is True
    created = _call("create_series", {"package": pkg})
    assert created["ok"] is True and created["episodes"]["created"] == [1, 2, 3]
    assert _call("create_series", {"package": pkg})["ok"] is False
    listed = _call("list_series", {})["series"]
    assert listed == [{"slug": "ballot-math", "name": "Ballot Math", "status": "draft", "episodes": 3}]
    got = _call("get_series", {"slug": "ballot-math"})
    assert got["episode_states"] == {"1": "planned", "2": "planned", "3": "planned"}
    assert _call("set_user_profile", {"content": "## Tone\n\nWarm"})["version"] == 1
    assert _call("get_user_profile", {})["content"].startswith("## Tone")

    bad = dict(pkg, plan=[dict(pkg["plan"][0], keyword="X"), dict(pkg["plan"][1], keyword="x")])
    failed = _call("update_series", {"package": bad})
    assert failed["ok"] is False and failed["issues"]


def test_episode_slot_fields_saved(db):
    with db.session_scope() as s:
        uid = current_user_id(s)
        save_package(s, uid, sample_package(), mode="create")
        e1, e2, e3 = get_series(s, uid, "ballot-math").episodes
        assert e1.slot_kind == SlotKind.PRECISE and e1.slot_time.hour == 19
        assert e2.slot_kind == SlotKind.WEEK and e2.slot_time is None
        assert e3.slot_kind == SlotKind.AFTER and e3.after_episode == 2
