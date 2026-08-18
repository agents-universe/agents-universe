"""request_user_selection must wake on abort — a prompt nobody can answer
(UI closed, queue full) must not block the agent for the full timeout."""
import asyncio

import pytest

from agent_core.session import ConversationSession


async def test_abort_wakes_prompt_immediately():
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection("p1", "field", "Approve?", timeout=300)
    )
    await asyncio.sleep(0.05)  # let it reach the wait

    s.abort()

    with pytest.raises(RuntimeError, match="[Aa]borted"):
        await asyncio.wait_for(task, timeout=2)
    assert "p1" not in s._pending_prompts


async def test_abort_before_prompt_also_wakes():
    s = ConversationSession("c1", "p1", "u1")
    s.abort()
    with pytest.raises(RuntimeError, match="[Aa]borted"):
        await s.request_user_selection("p2", "field", "Approve?", timeout=300)
    assert "p2" not in s._pending_prompts


async def test_normal_response_path_unchanged():
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection("p3", "field", "Approve?", timeout=30)
    )
    await asyncio.sleep(0.05)
    assert s.resolve_user_selection("p3", "yes")

    assert await task == "yes"
    assert "p3" not in s._pending_prompts
