"""Service-level tests for project-scoped agent sync."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from agent_core import definition_check

from api.models.agent import Agent
from api.models.conversation import Conversation
from api.services.agent_sync import sync_agents_dir


def _write_agent_file(agents_dir: Path, slug: str, display_name: str | None = None) -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{slug}.agent.md"
    path.write_text(
        f'---\nslug: "{slug}"\ndisplay_name: "{display_name or slug}"\n'
        f'tools: [filesystem]\n---\n\nBody for {slug}\n',
        encoding="utf-8",
    )
    return path


async def test_project_sync_registers_scoped_agent(db, tmp_path, make_project):
    project = await make_project("proj-a")
    agents_dir = tmp_path / "agents"
    _write_agent_file(agents_dir, "proj-a--helper", "Helper")

    synced, removed = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-a--",
    )

    assert synced == ["proj-a--helper"]
    assert removed == []
    row = (await db.execute(select(Agent).where(Agent.slug == "proj-a--helper"))).scalar_one()
    assert row.project_id == str(project.project_id)
    assert row.is_system is False
    assert row.definition_path.endswith("proj-a--helper.agent.md")
    assert row.display_name == "Helper"


async def test_project_sync_skips_slug_without_prefix(db, tmp_path, make_project):
    project = await make_project("proj-b")
    agents_dir = tmp_path / "agents"
    _write_agent_file(agents_dir, "no-prefix")

    synced, _ = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-b--",
    )

    assert synced == []
    assert (await db.execute(select(Agent).where(Agent.slug == "no-prefix"))).scalar_one_or_none() is None


async def test_project_sync_removes_missing_definition_and_nulls_conversations(db, tmp_path, make_project):
    project = await make_project("proj-c")
    agents_dir = tmp_path / "agents"
    _write_agent_file(agents_dir, "proj-c--gone", "Gone")
    await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-c--",
    )
    row = (await db.execute(select(Agent).where(Agent.slug == "proj-c--gone"))).scalar_one()
    conv = Conversation(project_id=str(project.project_id), user_id="u", agent_id=row.agent_id)
    db.add(conv)
    await db.commit()

    (agents_dir / "proj-c--gone.agent.md").unlink()
    synced, removed = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-c--",
    )

    assert synced == []
    assert removed == ["proj-c--gone"]
    conv_row = (await db.execute(
        select(Conversation).where(Conversation.conversation_id == conv.conversation_id)
    )).scalar_one()
    assert conv_row.agent_id is None


async def test_scope_isolation_between_global_and_project(db, tmp_path, make_project):
    project = await make_project("proj-d")
    agents_dir = tmp_path / "agents"
    _write_agent_file(agents_dir, "proj-d--x", "X")
    await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-d--",
    )

    # Global sync must not touch project rows.
    global_dir = tmp_path / "global"
    _write_agent_file(global_dir, "global-agent", "Global")
    await sync_agents_dir(db, global_dir, project_id=None, is_system=True)

    proj_row = (await db.execute(select(Agent).where(Agent.slug == "proj-d--x"))).scalar_one()
    assert proj_row.project_id == str(project.project_id)
    assert proj_row.is_system is False

    # Project sync must not remove global rows.
    await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-d--",
    )
    g = (await db.execute(select(Agent).where(Agent.slug == "global-agent"))).scalar_one()
    assert g.is_system is True
    assert g.project_id is None


async def test_missing_project_agents_dir_cleans_rows(db, tmp_path, make_project):
    project = await make_project("proj-e")
    agents_dir = tmp_path / "agents"
    _write_agent_file(agents_dir, "proj-e--y", "Y")
    await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-e--",
    )
    assert (await db.execute(
        select(Agent).where(Agent.slug == "proj-e--y")
    )).scalar_one_or_none() is not None

    # Workspace gone → all project rows cascade-removed.
    synced, removed = await sync_agents_dir(
        db, tmp_path / "does-not-exist", project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-e--",
    )
    assert synced == []
    assert removed == ["proj-e--y"]


async def test_sync_placeholder_and_starter_prompts(db, tmp_path, make_project):
    """Frontmatter guidance fields round-trip, including CJK text.

    json.dumps defaults to ensure_ascii, so Chinese prompts expand through
    the escape — the column is UnicodeText precisely for that.
    """
    project = await make_project("proj-h")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "proj-h--guide.agent.md").write_text(
        '---\nslug: "proj-h--guide"\ndisplay_name: "Guide"\n'
        'placeholder: "描述你想定制的智能体能力或行为"\n'
        "starter_prompts:\n"
        '  - "帮我给这个智能体新增一个技能"\n'
        '  - "检查这份智能体定义有什么问题"\n'
        "---\n\nBody\n",
        encoding="utf-8",
    )

    synced, _ = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-h--",
    )

    assert synced == ["proj-h--guide"]
    row = (await db.execute(select(Agent).where(Agent.slug == "proj-h--guide"))).scalar_one()
    assert row.placeholder == "描述你想定制的智能体能力或行为"
    assert json.loads(row.starter_prompts) == [
        "帮我给这个智能体新增一个技能",
        "检查这份智能体定义有什么问题",
    ]


async def test_sync_guidance_fields_absent_and_normalized(db, tmp_path, make_project):
    project = await make_project("proj-i")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)

    # No guidance fields → both columns NULL.
    _write_agent_file(agents_dir, "proj-i--plain", "Plain")
    # Scalar string → one-item list; non-list junk → []; overlong → truncated.
    (agents_dir / "proj-i--odd.agent.md").write_text(
        '---\nslug: "proj-i--odd"\ndisplay_name: "Odd"\n'
        'starter_prompts: "一个提示，含逗号"\n'
        'placeholder: "' + "x" * 600 + '"\n'
        "---\n\nBody\n",
        encoding="utf-8",
    )
    (agents_dir / "proj-i--junk.agent.md").write_text(
        '---\nslug: "proj-i--junk"\ndisplay_name: "Junk"\n'
        "starter_prompts: 42\n---\n\nBody\n",
        encoding="utf-8",
    )

    synced, _ = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-i--",
    )

    assert set(synced) == {"proj-i--plain", "proj-i--odd", "proj-i--junk"}
    plain = (await db.execute(select(Agent).where(Agent.slug == "proj-i--plain"))).scalar_one()
    assert plain.placeholder is None
    assert plain.starter_prompts is None

    # Never comma-split: the prompt contains a full-width comma and stays whole.
    odd = (await db.execute(select(Agent).where(Agent.slug == "proj-i--odd"))).scalar_one()
    assert json.loads(odd.starter_prompts) == ["一个提示，含逗号"]
    assert len(odd.placeholder) == 500

    junk = (await db.execute(select(Agent).where(Agent.slug == "proj-i--junk"))).scalar_one()
    assert junk.starter_prompts is None


async def test_sync_locale_map_fields_store_canonical_and_i18n(db, tmp_path, make_project):
    """Bilingual frontmatter: canonical columns keep the zh entry (sort key,
    history prefix), the i18n column carries every explicit locale map."""
    project = await make_project("proj-i18n")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "proj-i18n--bi.agent.md").write_text(
        "---\n"
        'slug: "proj-i18n--bi"\n'
        "display_name:\n  zh-CN: 技术负责人\n  en-US: Tech Lead\n"
        "description:\n  zh-CN: 实现侧的技术负责人\n  en-US: Implementation lead\n"
        "placeholder:\n  zh-CN: 粘贴 PR 链接…\n  en-US: Paste a PR link…\n"
        "starter_prompts:\n"
        "  zh-CN:\n    - 评审这个 PR\n"
        "  en-US:\n    - Review this PR\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )

    synced, _ = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-i18n--",
    )

    assert synced == ["proj-i18n--bi"]
    row = (await db.execute(select(Agent).where(Agent.slug == "proj-i18n--bi"))).scalar_one()
    assert row.display_name == "技术负责人"
    assert row.description == "实现侧的技术负责人"
    assert row.placeholder == "粘贴 PR 链接…"
    assert json.loads(row.starter_prompts) == ["评审这个 PR"]
    assert json.loads(row.i18n) == {
        "display_name": {"zh-CN": "技术负责人", "en-US": "Tech Lead"},
        "description": {"zh-CN": "实现侧的技术负责人", "en-US": "Implementation lead"},
        "placeholder": {"zh-CN": "粘贴 PR 链接…", "en-US": "Paste a PR link…"},
        "starter_prompts": {"zh-CN": ["评审这个 PR"], "en-US": ["Review this PR"]},
    }


async def test_sync_scalar_fields_leave_i18n_null(db, tmp_path, make_project):
    """Pre-i18n definitions (plain scalars) must not grow an i18n payload."""
    project = await make_project("proj-ni18n")
    agents_dir = tmp_path / "agents"
    _write_agent_file(agents_dir, "proj-ni18n--plain", "Plain")

    await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-ni18n--",
    )

    row = (await db.execute(select(Agent).where(Agent.slug == "proj-ni18n--plain"))).scalar_one()
    assert row.i18n is None


async def test_sync_locale_map_values_bounded_per_locale(db, tmp_path, make_project):
    """Each locale value obeys the same column widths — a map must not
    bypass the truncation guards the scalar path enforces."""
    project = await make_project("proj-cap")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "proj-cap--long.agent.md").write_text(
        "---\n"
        'slug: "proj-cap--long"\n'
        "display_name:\n"
        f'  zh-CN: "{"名" * 300}"\n'
        f'  en-US: "{"N" * 300}"\n'
        "---\n\nBody\n",
        encoding="utf-8",
    )

    await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-cap--",
    )

    row = (await db.execute(select(Agent).where(Agent.slug == "proj-cap--long"))).scalar_one()
    assert len(row.display_name) == 255
    i18n = json.loads(row.i18n)
    assert {loc: len(v) for loc, v in i18n["display_name"].items()} == {
        "zh-CN": 255,
        "en-US": 255,
    }


async def test_sync_non_locale_dict_description_degrades_to_null(db, tmp_path, make_project):
    """A structured dict with non-locale keys is invalid frontmatter — the
    canonical column degrades to NULL and the sync still succeeds."""
    project = await make_project("proj-badd")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "proj-badd--x.agent.md").write_text(
        "---\n"
        'slug: "proj-badd--x"\n'
        'display_name: "X"\n'
        "description:\n  slug: not-a-locale\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )

    synced, _ = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-badd--",
    )

    assert synced == ["proj-badd--x"]
    row = (await db.execute(select(Agent).where(Agent.slug == "proj-badd--x"))).scalar_one()
    assert row.description is None
    assert row.i18n is None


async def test_definition_check_predicts_registration(db, tmp_path, make_project):
    """The write-time verdict must match what sync actually does.

    The whole point of `definition_check` is that the model can trust it
    instead of waiting for a second round-trip: ok=True has to mean "this will
    be listed", ok=False has to mean "this will be skipped". Each case gets its
    own agents/ dir so one file cannot mask another.
    """
    project = await make_project("proj-f")
    cases = [
        ("proj-f--good", '---\nslug: "proj-f--good"\ndisplay_name: "Good"\n---\n', True),
        # Slug missing the project prefix → sync skips it silently.
        ("proj-f--bad", '---\nslug: "bad"\ndisplay_name: "Bad"\n---\n', False),
        # slug not equal to the filename stem → listed but unrunnable.
        ("proj-f--mismatch", '---\nslug: "proj-f--other"\ndisplay_name: "M"\n---\n', False),
        # Unquoted colon → YAML parse error.
        ("proj-f--yaml", '---\nslug: "proj-f--yaml"\ndescription: 负责 助手: 做事\n---\n', False),
    ]

    for stem, content, expect_ok in cases:
        agents_dir = tmp_path / stem / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / f"{stem}.agent.md").write_text(content, encoding="utf-8")

        check = definition_check.check_definition(
            f"agents/{stem}.agent.md", content, scope="project", project_slug="proj-f"
        )
        assert check["ok"] is expect_ok, stem

        synced, _ = await sync_agents_dir(
            db, agents_dir, project_id=str(project.project_id),
            is_system=False, slug_prefix="proj-f--",
        )
        # A mismatched slug still syncs — under the *frontmatter* slug, which
        # resolves to no file at runtime. "Not registered under its own name"
        # is the accurate statement for every ok=False case.
        assert (stem in synced) is expect_ok, stem


async def test_overlong_scalars_are_capped_and_overlong_slug_skipped(
    db, tmp_path, make_project
):
    """Frontmatter is user-editable and unbounded. An over-long list used to
    fail the whole sync commit; an over-long slug cannot be stored at all."""
    project = await make_project("proj-long")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    long_slug = "proj-long--" + "a" * 110
    (agents_dir / f"{long_slug}.agent.md").write_text(
        f'---\nslug: "{long_slug}"\ndisplay_name: "Too long"\n---\n\nBody\n',
        encoding="utf-8",
    )
    many_tools = ", ".join(f'"tool-{i:03d}-abcdefgh"' for i in range(100))
    (agents_dir / "proj-long--bounded.agent.md").write_text(
        '---\nslug: "proj-long--bounded"\ndisplay_name: "' + "名" * 300 + '"\n'
        f"tools: [{many_tools}]\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )

    synced, _ = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-long--",
    )

    assert synced == ["proj-long--bounded"]
    row = (await db.execute(
        select(Agent).where(Agent.slug == "proj-long--bounded")
    )).scalar_one()
    assert len(row.display_name) == 255
    assert row.tools is not None
    assert len(row.tools) <= 1000
    parsed = json.loads(row.tools)
    assert parsed and parsed[0] == "tool-000-abcdefgh"


async def test_commit_failure_reports_nothing_synced(db, tmp_path, make_project, monkeypatch):
    """A failed commit rolls back every insert/update/delete; returning the
    attempted slug lists made callers believe the sync applied."""
    project = await make_project("proj-sync-fail")
    agents_dir = tmp_path / "agents"
    _write_agent_file(agents_dir, "proj-sync-fail--kept")
    await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-sync-fail--",
    )

    # Replace the kept definition with a new one so the next sync both
    # inserts and deletes in one transaction.
    (agents_dir / "proj-sync-fail--kept.agent.md").unlink()
    _write_agent_file(agents_dir, "proj-sync-fail--new")

    async def _boom():
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(db, "commit", _boom)
    synced, removed = await sync_agents_dir(
        db, agents_dir, project_id=str(project.project_id),
        is_system=False, slug_prefix="proj-sync-fail--",
    )
    monkeypatch.undo()

    assert synced == []
    assert removed == []
    # Rollback left the previously synced row in place; the new one is absent.
    kept = (await db.execute(
        select(Agent).where(Agent.slug == "proj-sync-fail--kept")
    )).scalar_one_or_none()
    new = (await db.execute(
        select(Agent).where(Agent.slug == "proj-sync-fail--new")
    )).scalar_one_or_none()
    assert kept is not None
    assert new is None
