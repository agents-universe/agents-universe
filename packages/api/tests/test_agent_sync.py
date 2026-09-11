"""Service-level tests for project-scoped agent sync."""
from __future__ import annotations

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
