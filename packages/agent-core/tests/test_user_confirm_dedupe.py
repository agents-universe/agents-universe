"""A question the user already answered must not be asked a second time.

The session records how each interactive prompt ended (answered / timed out /
dismissed) for one turn, keyed by the caller's field key plus the normalized
question; ``user_confirm`` consults that ledger before opening a dialog. Without
it every model iteration re-emits the same prompt, and a prompt that expired
unanswered invites the next iteration to ask again — the "每次都选了它还问" loop.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_core.session import (
    ConversationSession,
    UserInputEntry,
    UserSelectionTimeoutError,
)
from agent_core.tools.base import ToolContext
from agent_core.tools.user_confirm import UserConfirmTool

SCOPE = {
    "question": "Which environment is authorized for testing?",
    "field_key": "pentest_scope_tier",
    "options": [{"label": "dev", "value": "dev"}, {"label": "uat", "value": "uat"}],
}
ENTRY_POINTS = {
    "question": "Which entry points are in scope?",
    "field_key": "pentest_entry_points",
}
SECRET = {
    "question": "Password for the pentest admin account",
    "field_key": "pentest:admin:password",
    "secret": True,
    "service_key": "pentest:admin:password",
    "save_to_user_tokens": True,
}


def _ctx(session) -> ToolContext:
    return ToolContext(
        project_id="p", project_fs_path=".", conversation_id="c", user_id="u",
        session=session,
    )


class _EventLog:
    """Drains a session's event queue so tests can count emitted prompts."""

    def __init__(self, session: ConversationSession):
        self.events: list = []
        self._session = session
        self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        async for ev in self._session.events():
            self.events.append(ev)

    async def finish(self) -> list:
        await self._session.close()
        await asyncio.wait_for(self._task, timeout=2)
        return self.events


def _prompts(events: list) -> list:
    return [e for e in events if e.type == "user_selection_required"]


async def _answer_next_prompt(session: ConversationSession, value: str) -> None:
    """Resolve the first prompt the tool emitted with *value*."""
    for _ in range(200):
        if session._pending_prompts:  # noqa: SLF001 - the emit is the observable here
            prompt_id = next(iter(session._pending_prompts))
            session.resolve_user_selection(prompt_id, value)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no prompt was emitted")


@pytest.mark.asyncio
async def test_answered_question_is_reused_instead_of_re_prompted():
    s = ConversationSession("c1", "p1", "u1")
    log = _EventLog(s)
    tool, ctx = UserConfirmTool(), _ctx(s)

    first = asyncio.create_task(tool.execute(SCOPE, ctx))
    await _answer_next_prompt(s, "uat")
    assert (await first)["selected_value"] == "uat"

    second = await tool.execute(SCOPE, ctx)
    assert second["reused"] is True
    assert second["selected_value"] == "uat"

    assert len(_prompts(await log.finish())) == 1


@pytest.mark.asyncio
async def test_question_whitespace_and_case_hits_the_same_entry():
    s = ConversationSession("c1", "p1", "u1")
    tool, ctx = UserConfirmTool(), _ctx(s)

    first = asyncio.create_task(tool.execute(SCOPE, ctx))
    await _answer_next_prompt(s, "dev")
    await first

    reworded = {**SCOPE, "question": "  which   ENVIRONMENT is authorized for testing? "}
    assert (await tool.execute(reworded, ctx))["reused"] is True


@pytest.mark.asyncio
async def test_different_question_on_same_field_key_is_still_asked():
    """Suppression is per decision, not per field key.

    Keying on the field key alone would hand back the tier answer to a later
    production-authorization question that declares the same key.
    """
    s = ConversationSession("c1", "p1", "u1")
    log = _EventLog(s)
    tool, ctx = UserConfirmTool(), _ctx(s)

    first = asyncio.create_task(tool.execute(SCOPE, ctx))
    await _answer_next_prompt(s, "dev")
    await first

    second = asyncio.create_task(tool.execute(
        {**SCOPE, "question": "Confirm ACTIVE testing against production?"}, ctx
    ))
    await _answer_next_prompt(s, "no")
    assert (await second)["selected_value"] == "no"

    assert len(_prompts(await log.finish())) == 2


@pytest.mark.asyncio
async def test_prompt_without_field_key_is_always_shown():
    """No key means no decision identity — suppress nothing."""
    s = ConversationSession("c1", "p1", "u1")
    log = _EventLog(s)
    tool, ctx = UserConfirmTool(), _ctx(s)
    params = {"question": "Continue?"}

    for _ in range(2):
        task = asyncio.create_task(tool.execute(params, ctx))
        await _answer_next_prompt(s, "yes")
        assert (await task)["selected_value"] == "yes"

    assert len(_prompts(await log.finish())) == 2


@pytest.mark.asyncio
async def test_timed_out_question_is_not_reasked(monkeypatch):
    s = ConversationSession("c1", "p1", "u1")
    calls: list[dict] = []

    async def _timeout(**kwargs):
        calls.append(kwargs)
        raise UserSelectionTimeoutError("timed out after 300 s")

    monkeypatch.setattr(s, "request_user_selection", _timeout)
    tool, ctx = UserConfirmTool(), _ctx(s)

    first = await tool.execute(SCOPE, ctx)
    assert first["timed_out"] is True
    assert first["do_not_reask"] is True

    second = await tool.execute(SCOPE, ctx)
    assert second["timed_out"] is True
    assert second["do_not_reask"] is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_pauses_further_prompts(monkeypatch):
    """One expired prompt stops prompting for the rest of the turn."""
    s = ConversationSession("c1", "p1", "u1")
    calls: list[dict] = []

    async def _timeout(**kwargs):
        calls.append(kwargs)
        raise UserSelectionTimeoutError("timed out after 300 s")

    monkeypatch.setattr(s, "request_user_selection", _timeout)
    tool, ctx = UserConfirmTool(), _ctx(s)

    await tool.execute(SCOPE, ctx)
    assert s.interactive_prompts_paused is True

    other = await tool.execute(ENTRY_POINTS, ctx)
    assert other["prompts_paused"] is True
    assert other["do_not_reask"] is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mid_run_user_message_lifts_the_pause():
    """The user replying mid-run is present again — prompting may resume."""
    s = ConversationSession("c1", "p1", "u1")
    log = _EventLog(s)
    tool, ctx = UserConfirmTool(), _ctx(s)

    s.note_prompt_timeout()
    assert s.interactive_prompts_paused is True

    assert s.enqueue_user_input(
        UserInputEntry(message_id="m1", content="here — use uat", attachments=[])
    )
    assert s.interactive_prompts_paused is False

    task = asyncio.create_task(tool.execute(SCOPE, ctx))
    await _answer_next_prompt(s, "uat")
    assert (await task)["selected_value"] == "uat"

    assert len(_prompts(await log.finish())) == 1


@pytest.mark.asyncio
async def test_dismissed_prompt_is_not_reasked():
    s = ConversationSession("c1", "p1", "u1")
    log = _EventLog(s)
    tool, ctx = UserConfirmTool(), _ctx(s)

    first = asyncio.create_task(tool.execute(SCOPE, ctx))
    await _answer_next_prompt(s, "__cancelled__")
    dismissed = await first
    assert dismissed["cancelled"] is True
    assert dismissed["do_not_reask"] is True

    second = await tool.execute(SCOPE, ctx)
    assert second["dismissed"] is True
    assert second["do_not_reask"] is True

    assert len(_prompts(await log.finish())) == 1


@pytest.mark.asyncio
async def test_force_asks_again_and_updates_the_recorded_answer():
    """The escape hatch for "actually, change my answer"."""
    s = ConversationSession("c1", "p1", "u1")
    tool, ctx = UserConfirmTool(), _ctx(s)

    first = asyncio.create_task(tool.execute(SCOPE, ctx))
    await _answer_next_prompt(s, "dev")
    await first

    forced = asyncio.create_task(tool.execute({**SCOPE, "force": True}, ctx))
    await _answer_next_prompt(s, "uat")
    assert (await forced)["selected_value"] == "uat"

    assert (await tool.execute(SCOPE, ctx))["selected_value"] == "uat"


@pytest.mark.asyncio
async def test_secret_prompts_are_never_replayed():
    """A replayed "secret_saved" would claim a stored value that does not exist."""
    s = ConversationSession("c1", "p1", "u1")
    log = _EventLog(s)
    tool, ctx = UserConfirmTool(), _ctx(s)

    for _ in range(2):
        task = asyncio.create_task(tool.execute(SECRET, ctx))
        await _answer_next_prompt(s, "secret_saved")
        assert (await task)["status"] == "secret_saved"

    assert len(_prompts(await log.finish())) == 2


@pytest.mark.asyncio
async def test_parallel_same_question_shows_one_dialog():
    """Concurrent callers asking one question must not stack two dialogs."""
    s = ConversationSession("c1", "p1", "u1")
    log = _EventLog(s)
    tool, ctx = UserConfirmTool(), _ctx(s)

    both = [asyncio.create_task(tool.execute(SCOPE, ctx)) for _ in range(2)]
    await asyncio.sleep(0.05)
    assert len(s._pending_prompts) == 1

    await _answer_next_prompt(s, "dev")
    results = [await t for t in both]
    assert [r["selected_value"] for r in results] == ["dev", "dev"]
    assert sorted(r.get("reused", False) for r in results) == [False, True]

    assert len(_prompts(await log.finish())) == 1
