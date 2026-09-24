"""The knowledge_rw post-op hook must invalidate the static prompt cache.

``_get_static_prompt`` embeds ``ctx.loaded_content`` verbatim into the cached
static string, so ``_mark_prompt_dirty`` alone is not enough: the rebuild the
dirty flag triggers returns the SAME cache while it is still set. delete /
refresh / purge mutate that content through the tool, so the cache must be
dropped — otherwise the "rebuilt" prompt keeps serving deleted or stale
knowledge, exactly what the hook's comment warns against.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.knowledge.loader import KnowledgeContextResult
from agent_core.providers.base import Message, StopReason, StreamChunk
from agent_core.session import ConversationSession


class _DrainingSession(ConversationSession):
    """Session that auto-drains its event queue so emit() never blocks."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.events_emitted: list[tuple[str, dict]] = []
        self._drainer: asyncio.Task | None = None

    def start_drainer(self):
        async def _drain():
            async for evt in self.events():
                self.events_emitted.append((evt.type, dict(evt.data)))
        self._drainer = asyncio.create_task(_drain())

    async def stop_drainer(self):
        await self.close()
        if self._drainer:
            await self._drainer


def _make_agent(tmp_path) -> Agent:
    config = AgentConfig(
        slug="test",
        description="test",
        system_prompt="You are a test agent.",
    )
    tool_ctx = MagicMock()
    tool_ctx.project_fs_path = str(tmp_path)
    tool_ctx.copy_for_task = MagicMock(return_value=tool_ctx)
    tool_ctx.cleanup = AsyncMock()
    return Agent(
        config=config,
        credentials={"cfg1": {"api_key": "test-key"}},
        tier_models={"cfg1": {"provider": "openai", "model": "test-model"}},
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
        project_context=KnowledgeContextResult(),
    )


class _FakeKnowledgeRW:
    """knowledge_rw stand-in mirroring delete's ctx mutation (pop the slug)."""

    name = "knowledge_rw"
    description = "knowledge tool"
    parameters = {"type": "object", "properties": {}}
    prompt_hint = "knowledge"

    def __init__(self, ctx):
        self.ctx = ctx

    async def execute(self, params: dict, context) -> dict:
        self.ctx.loaded_content.pop(params.get("slug", ""), None)
        return {"success": True, "slug": params.get("slug")}

    def to_definition(self):
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@pytest.mark.asyncio
async def test_knowledge_rw_delete_drops_stale_static_prompt_cache(tmp_path):
    agent = _make_agent(tmp_path)
    ctx = agent._project_context
    ctx.loaded_content["technical/foo"] = "old content"
    # A cache built BEFORE the delete — it embeds the file's content.
    agent._static_prompt_cache = (
        "## Project Knowledge\n### [[technical/foo]]\nold content"
    )
    agent._tools["knowledge_rw"] = _FakeKnowledgeRW(ctx)

    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    provider_calls = 0

    async def _stream(messages, tools=None, max_tokens=4096, temperature=0.0):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            yield StreamChunk(
                tool_call_delta={
                    "index": 0,
                    "id": "call-1",
                    "function": {
                        "name": "knowledge_rw",
                        "arguments": '{"operation": "delete", "slug": "technical/foo"}',
                    },
                },
                stop_reason=StopReason.END_TURN,
            )
        else:
            yield StreamChunk(delta="Deleted.", stop_reason=StopReason.END_TURN)

    provider = MagicMock()
    provider.stream = _stream
    provider.model_name = "fake"
    provider.supports_vision = False
    provider.context_window = 100000

    await agent._run_loop(
        [
            Message(role="system", content="seed system"),
            Message(role="user", content="Initial"),
        ],
        [],
        provider,
        session,
        "cfg1",
    )
    await session.stop_drainer()

    # The dirty flag alone does NOT rebuild the static portion: the rebuild
    # returns _static_prompt_cache while it is set — so stale deleted content
    # would keep shipping to the model.
    assert agent._static_prompt_cache is None or "old content" not in agent._static_prompt_cache, (
        agent._static_prompt_cache
    )
