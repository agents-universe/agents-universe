"""Agent-as-a-Service runtime: SSE conversation orchestration for published agents.

A published agent exposes its project resources to external callers over SSE.
The conversation is owned by the *publisher* (``conversations.user_id =
owner_id``) so every run — regardless of who calls — is attributed to and
billed against the publisher's model config, and is marked
``source='publish'`` so it never appears in the publisher's sidebar.

Security invariants (see credential-leak-redaction memory):

- API keys are stored as SHA-256 hashes + a 4-char hint; the plaintext is
  shown exactly once at issue time and never logged, echoed, or persisted.
- Key comparison is constant-time (``hashlib.compare_digest``).
- Non-interactive runs degrade to a readable error instead of blocking on a
  human prompt (``interactive=False`` through the turn kernel).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

_log = logging.getLogger("agents_universe.publish")

# External key namespace prefix — the raw key carries the publish id so a
# single lookup finds the right row without scanning all keys.
_PUBLISH_KEY_PREFIX = "pua_"


def generate_publish_key(publish_id: str) -> str:
    """Return a fresh API key for *publish_id* (shown once to the publisher).

    The key embeds the publish id as a routing hint: ``pua_<pubid>_<secret>``.
    Only the SHA-256 of the full key is stored.
    """
    return f"{_PUBLISH_KEY_PREFIX}{publish_id}_{secrets.token_urlsafe(32)}"


def hash_publish_key(key: str) -> str:
    """SHA-256 of the full key, hex digest (compare via compare_digest)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def publish_key_hint(key: str) -> str:
    """Last 4 chars of the secret tail for display."""
    return "..." + key[-4:] if len(key) > 4 else "****"


async def authenticate_publish_key(db, key: str):
    """Resolve *key* to (publish_id, key_row) or None.

    The key's ``pua_<publish_id>_`` prefix routes straight to the owning
    publish; the stored SHA-256 hash is compared in constant time.
    """
    from sqlalchemy import select

    from api.models.publish import PublishKey

    if not key.startswith(_PUBLISH_KEY_PREFIX):
        return None
    try:
        _, pubid, _ = key.split("_", 2)
    except ValueError:
        return None
    if not pubid:
        return None
    result = await db.execute(
        select(PublishKey).where(
            PublishKey.publish_id == pubid,
            PublishKey.is_active == True,  # noqa: E712
        )
    )
    for row in result.scalars().all():
        if hmac.compare_digest(row.key_hash, hash_publish_key(key)):
            return pubid, row
    return None


@dataclass
class SSEStream:
    """Async-queue-backed SSE event stream handed to a turn as its transport.

    ``run_turn`` pushes dict events here via ``send``; the HTTP handler drains
    the queue into the ``text/event-stream`` response. The queue is bounded so
    a slow client cannot balloon memory — if the client stalls past the
    buffer, the turn's pushes drop (False) instead of blocking forever.
    """

    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))

    async def send(self, conversation_id: str, data: dict) -> bool:
        try:
            self.queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            _log.warning("SSE queue full for %s — dropping event", conversation_id)
            return False


def sse_format(data: dict) -> str:
    """Render a turn event as one SSE ``data:`` frame (JSON, no id/retry)."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def get_or_create_publish_conversation(
    db, publish, *, title: str | None = None
) -> str:
    """Return the conversation id for a published agent, creating it on first use.

    One durable conversation per publish. ``source='publish'`` keeps it out of
    the publisher's sidebar; user_id = owner so the turn kernel's ownership
    checks pass and history is attributed to the publisher.
    """
    from sqlalchemy import select

    from api.models.agent import Agent
    from api.models.conversation import Conversation

    # Resolve the agent's DB row (project-scoped agents resolve within the
    # publish's project).
    result = await db.execute(
        select(Agent).where(Agent.slug == publish.agent_slug)
    )
    agent = result.scalar_one_or_none()
    agent_id = agent.agent_id if agent else None

    # Reuse the existing publish conversation when present.
    existing = await db.execute(
        select(Conversation).where(
            Conversation.project_id == publish.project_id,
            Conversation.user_id == publish.owner_id,
            Conversation.source == "publish",
            Conversation.status == "active",
        ).order_by(Conversation.created_at.desc()).limit(1)
    )
    conv = existing.scalar_one_or_none()
    if conv:
        return str(conv.conversation_id)

    conv = Conversation(
        conversation_id=str(uuid.uuid4()),
        project_id=publish.project_id,
        agent_id=agent_id,
        user_id=publish.owner_id,
        source="publish",
        title=title or publish.title or f"发布: {publish.agent_slug}",
    )
    db.add(conv)
    await db.commit()
    return str(conv.conversation_id)


async def _get_publish_agent(db, publish) -> dict | None:
    """Agent display row for a publish, or None when the definition vanished."""
    from api.models.agent import Agent

    result = await db.execute(
        select(Agent)
        .where(Agent.slug == publish.agent_slug)
        .order_by(Agent.project_id.is_(None))  # global wins for same slug
    )
    agent = result.scalars().first()
    if agent is None:
        return None
    return {
        "slug": agent.slug,
        "display_name": agent.display_name,
        "description": agent.description,
        "project_id": agent.project_id,
    }


async def get_publish_viewer_payload(db, publish, viewer_id: str) -> dict:
    """Public page payload for a publish, scoped to the logged-in viewer.

    Every run on the shared publish conversation executes as the publisher,
    so the conversation row (and its history) is attributed to the publisher.
    The viewer NEVER owns it — instead they get a *viewer token* that lets
    the /session endpoints resolve and run that conversation. The token is
    derived from a server secret (not stored), so there is nothing to leak
    or revoke.
    """
    from api.models.conversation import Conversation

    agent = await _get_publish_agent(db, publish)
    conv_result = await db.execute(
        select(Conversation.conversation_id).where(
            Conversation.project_id == publish.project_id,
            Conversation.user_id == publish.owner_id,
            Conversation.source == "publish",
            Conversation.status == "active",
        )
    )
    conversation_id = conv_result.scalar_one_or_none()
    return {
        "publish_id": str(publish.publish_id),
        "agent": agent,
        "project_id": str(publish.project_id),
        "title": publish.title,
        "description": publish.description,
        "has_conversation": bool(conversation_id),
        # Token binding this viewer to the shared publish conversation. The
        # WS /session paths exchange it for run rights under the publisher.
        "token": publish_viewer_token(str(publish.publish_id), viewer_id),
    }


def publish_viewer_token(publish_id: str, viewer_id: str) -> str:
    """HMAC token proving *viewer_id* may open the publish conversation.

    Derived from the server secret — the same secret used to sign the
    WebSocket-cookie path. No DB storage; recomputed on every check.
    """
    import hashlib
    import hmac

    from api.config import get_settings

    secret = get_settings().secret_key.encode("utf-8")
    msg = f"publish:{publish_id}:viewer:{viewer_id}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


async def authorize_publish_viewer(
    db, publish_id: str, viewer_id: str, token: str
):
    """Verify *token* opens *publish_id* for *viewer_id*; return the publish."""
    from api.models.publish import AgentPublish

    if token is None:
        return None
    result = await db.execute(
        select(AgentPublish).where(
            AgentPublish.publish_id == publish_id,
            AgentPublish.page_enabled == True,  # noqa: E712
        )
    )
    publish = result.scalar_one_or_none()
    if publish is None:
        return None
    if not hmac.compare_digest(publish_viewer_token(publish_id, viewer_id), token):
        return None
    return publish


async def _serialize_publish_messages(db, publish, conversation_id: str, limit: int = 500):
    """Messages of a publish conversation in the frontend's serialized shape."""
    from api.models.conversation import Message as DbMessage

    result = await db.execute(
        select(DbMessage)
        .where(DbMessage.conversation_id == conversation_id)
        .order_by(DbMessage.sequence_num.desc())
        .limit(limit)
    )
    messages = list(result.scalars().all())
    messages.reverse()
    out = []
    for m in messages:
        out.append({
            "message_id": str(m.message_id),
            "role": m.role,
            "content": m.content or "",
            "agent_slug": m.agent_slug,
            "model_name": m.model_name,
            "tool_calls": _parse_json_col(m.tool_calls, []),
            "images": _parse_json_col(m.knowledge_refs, None),
            "attachments": None,
            "interrupted": False,
            "error": False,
            "sequence_num": m.sequence_num,
            "created_at": m.created_at.isoformat(),
        })
    return out


def _parse_json_col(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


async def run_published_turn(
    request,
    db,
    publish,
    conversation_id: str,
    message: str,
    *,
    user_hint: str | None = None,
) -> SSEStream:
    """Run one turn of a published agent and return an SSEStream to drain.

    Executes as the publisher (``actor_user_id=owner_id``) with their bound
    model config (``fixed_config_id``) and no human interaction. The turn
    kernel persists events; the caller drains and forwards them.
    """
    from types import SimpleNamespace

    from api.services.agent_turn import run_turn

    stream = SSEStream()

    # The kernel reads registries off ws.app.state — reuse the process-wide
    # ones loaded by the app lifespan (same as a WS turn).
    ws_stub = SimpleNamespace(app=request.app)

    msg = {
        "content": message,
        "fixed_config_id": publish.model_config_id,
    }
    await run_turn(
        conversation_id,
        ws=ws_stub,
        msg=msg,
        user_id=publish.owner_id,
        transport=stream,
        interactive=False,
        actor_user_id=publish.owner_id,
    )
    return stream
