"""user_confirm tool — pause the agent and show the user an interactive selection card."""
from __future__ import annotations

import uuid
from typing import Any

from agent_core.session import UserSelectionTimeoutError

from .base import Tool, ToolContext

# Answers returned in place of a dialog the user already answered, and the
# stoppage instructions returned when prompting must not be repeated.
_REUSED_NOTE = (
    "Reused the answer the user already gave for this decision. Do not ask it "
    "again; if the user explicitly asks to change it, call user_confirm again "
    "with force=true."
)
_TIMEOUT_REASK_ERROR = (
    "The user did not answer this prompt before it expired. Do not call "
    "user_confirm for this question again — stop the active work, ask it in "
    "plain chat text, and end the turn."
)
_DISMISS_REASK_ERROR = (
    "The user dismissed this prompt without answering. Do not call user_confirm "
    "for this question again — ask it in plain chat text and end the turn."
)
_PROMPTS_PAUSED_ERROR = (
    "An earlier prompt expired unanswered, so interactive prompts are paused for "
    "the rest of this turn. Do not call user_confirm again — continue with what "
    "the answers you have allow, or ask in plain chat text and end the turn."
)


def _normalize_question(question: str) -> str:
    return " ".join(question.split()).casefold()


def _prompt_signature(field_key: str, question: str) -> tuple[str, str] | None:
    """Identity of one decision, used to suppress repeat prompts.

    Keyed on the field key plus the normalized question: agents re-emit the
    same question with different spacing or casing between iterations, while a
    different question on the same field (a production authorization after a
    tier choice, say) must stay askable. Returns None when no field key is
    given — there is nothing to key the decision on then.
    """
    key = field_key.strip().casefold()
    if not key:
        return None
    return key, _normalize_question(question)


def _suppression_result(
    session: Any,
    signature: tuple[str, str] | None,
    field_key: str,
    force: bool,
) -> dict[str, Any] | None:
    """A result to return *instead of* prompting, or None to show the dialog.

    Order matters: a recorded outcome is the most specific thing we can say
    (it may be the user's actual choice), while the pause is the broader
    "stop prompting" state that follows an unanswered prompt.
    """
    if not force and signature is not None:
        outcome = session.get_prompt_outcome(signature)
        if outcome is not None:
            if outcome.status == "answered":
                return {
                    "field_key": field_key,
                    "selected_value": outcome.value,
                    "reused": True,
                    "note": _REUSED_NOTE,
                }
            if outcome.status == "timed_out":
                return {
                    "error": _TIMEOUT_REASK_ERROR,
                    "field_key": field_key,
                    "timed_out": True,
                    "do_not_reask": True,
                }
            return {
                "error": _DISMISS_REASK_ERROR,
                "field_key": field_key,
                "dismissed": True,
                "do_not_reask": True,
            }
    if session.interactive_prompts_paused:
        return {
            "error": _PROMPTS_PAUSED_ERROR,
            "field_key": field_key,
            "prompts_paused": True,
            "do_not_reask": True,
        }
    return None


class UserConfirmTool(Tool):
    """Present the user with a selection dialog and wait for their choice.

    The agent pauses until the user picks an option or enters custom text.
    Any agent that needs interactive confirmation adds this to its tools list.
    """

    prompt_hint = (
        "Pause to ask the user — choices, free text, or secure secret entry. Use it for "
        "decisions only the user can make (or required confirmations) instead of guessing."
    )

    @property
    def name(self) -> str:
        return "user_confirm"

    @property
    def description(self) -> str:
        return (
            "Pause and show the user an inline prompt. Supports selection cards, free-text input, "
            "and secure secret collection. For secrets, plaintext is never returned — the value "
            "is saved server-side to project secrets or user tokens and only an opaque status is returned. "
            "Give each decision its own field_key: asking a question the user already answered on "
            "that field returns the recorded answer instead of prompting again."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user, shown above the options.",
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["label", "value"],
                    },
                    "description": "Options to present. Can be empty for a free-text-only prompt.",
                },
                "field_key": {
                    "type": "string",
                    "description": (
                        "Identifier for the decision being confirmed, e.g. 'jira_project_key'. "
                        "One decision, one stable key: asking the same question on the same key "
                        "returns the answer already given this turn instead of prompting again."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Set true only when the user explicitly asks to answer this question again "
                        "or change an answer they already gave; bypasses duplicate suppression."
                    ),
                },
                "allow_other": {
                    "type": "boolean",
                    "description": "Whether to show an 'Other / custom input' option. Defaults to true.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["selection", "text"],
                    "description": "Prompt kind: 'selection' (radio options) or 'text' (free-text input). Default: selection.",
                },
                "title": {
                    "type": "string",
                    "description": "Dialog title displayed above the question.",
                },
                "message": {
                    "type": "string",
                    "description": "Additional context message shown in the dialog.",
                },
                "secret": {
                    "type": "boolean",
                    "description": "If true, show a password input. Value is saved server-side, never returned to the agent.",
                },
                "service_key": {
                    "type": "string",
                    "description": "Service key for secret storage (required when secret=true). e.g. 'third_party:crm:uat'.",
                },
                "environment": {
                    "type": "string",
                    "description": "Environment qualifier for the secret (e.g. 'dev', 'uat', 'prd').",
                },
                "save_to_project_secrets": {
                    "type": "boolean",
                    "description": "If true, save the collected secret to project secrets. Mutually exclusive with save_to_user_tokens.",
                },
                "save_to_user_tokens": {
                    "type": "boolean",
                    "description": "If true, save the collected secret to the user's personal key vault (user_tokens). Mutually exclusive with save_to_project_secrets.",
                },
            },
            "required": ["question"],
        }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        # Headless runs (published agents) must never block on a prompt nobody
        # can answer: return a readable error the model can act on instead.
        if not context.interactive:
            return {
                "error": (
                    "user_confirm is unavailable in non-interactive runs. "
                    "This agent is running unattended — do not ask the user "
                    "for input; pick the safe default and continue."
                ),
            }
        if context.session is None:
            return {
                "error": "user_confirm requires an active conversation session. "
                         "Ensure the tool is called from within an agent run.",
            }

        is_secret = params.get("secret", False)
        service_key = params.get("service_key", "")
        save_to_project = params.get("save_to_project_secrets", False)
        save_to_user = params.get("save_to_user_tokens", False)

        if is_secret and not service_key:
            return {"error": "service_key is required when secret=true"}
        if is_secret and not save_to_project and not save_to_user:
            return {"error": "Either save_to_project_secrets or save_to_user_tokens must be true when secret=true"}
        if is_secret and save_to_project and save_to_user:
            return {"error": "save_to_project_secrets and save_to_user_tokens are mutually exclusive"}

        question = params["question"]
        options = params.get("options", [])
        # The schema declares an array of {label, value} — a stringified list
        # would break the dialog rendering. Reject instead of mis-prompting.
        if isinstance(options, str):
            return {"error": "options must be an array of {label, value} objects, not a string"}
        if not isinstance(options, list):
            return {"error": "options must be an array of {label, value} objects"}
        normalized_options: list[dict] = []
        for option in options:
            # A bare string renders as a blank button (label/value undefined),
            # so reject rather than prompt with an unusable dialog.
            if not isinstance(option, dict) or not option.get("label"):
                return {
                    "error": "each option must be an object with a 'label' (and 'value')"
                }
            # A missing value leaves the button unselectable (the dialog
            # requires a non-empty selection) — the label is the value then.
            normalized_options.append({
                **option,
                "value": option.get("value") or option["label"],
            })
        options = normalized_options
        field_key = params.get("field_key", service_key or "")
        allow_other = params.get("allow_other", True)
        force = bool(params.get("force", False))
        # Secrets never join the ledger: a replayed "secret_saved" would claim a
        # stored value that does not exist, and re-collecting is always valid.
        signature = None if is_secret else _prompt_signature(field_key, question)

        session = context.session
        suppressed = _suppression_result(session, signature, field_key, force)
        if suppressed is not None:
            return suppressed

        async def _prompt() -> dict[str, Any]:
            prompt_id = str(uuid.uuid4())

            kwargs: dict[str, Any] = {
                "prompt_id": prompt_id,
                "field_key": field_key,
                "question": question,
                "options": options,
                "allow_other": allow_other,
            }

            if params.get("kind"):
                kwargs["kind"] = params["kind"]
            if params.get("title"):
                kwargs["title"] = params["title"]
            if params.get("message"):
                kwargs["message"] = params["message"]
            if is_secret:
                kwargs["secret"] = True
                kwargs["service_key"] = service_key
                if save_to_user:
                    kwargs["save_to_user_tokens"] = True
                else:
                    kwargs["save_to_project_secrets"] = True
            if params.get("environment"):
                kwargs["environment"] = params["environment"]

            try:
                selected_value = await session.request_user_selection(**kwargs)
            except UserSelectionTimeoutError:
                session.record_prompt_outcome(signature, "timed_out")
                session.note_prompt_timeout()
                return {
                    "error": _TIMEOUT_REASK_ERROR,
                    "field_key": field_key,
                    "timed_out": True,
                    "do_not_reask": True,
                }
            except RuntimeError as exc:
                # An aborted run, or a session implementation outside this
                # framework: report it without recording, the run is ending.
                return {"error": str(exc)}

            if is_secret:
                # The frontend cancels with value='__cancelled__' on a *non-secret*
                # response frame, so it never reaches the secret-save path — the
                # old code then reported "secret_saved" even though the user
                # dismissed the dialog and no secret was stored.
                if selected_value == "__cancelled__":
                    return {
                        "field_key": field_key,
                        "secret_ref": service_key,
                        "secret_scope": "user" if save_to_user else "project",
                        "cancelled": True,
                        "status": "cancelled",
                    }
                return {
                    "field_key": field_key,
                    "secret_ref": service_key,
                    "secret_scope": "user" if save_to_user else "project",
                    # Only two statuses exist (session.py resolve_user_selection_secret);
                    # anything else is a protocol drift — report failure rather than
                    # claiming a secret was stored when we cannot confirm it.
                    "status": selected_value if selected_value in ("secret_saved", "secret_save_failed") else "secret_save_failed",
                }

            if selected_value == "__cancelled__":
                session.record_prompt_outcome(signature, "dismissed")
                return {
                    "field_key": field_key,
                    "cancelled": True,
                    "selected_value": None,
                    "do_not_reask": True,
                    "note": _DISMISS_REASK_ERROR,
                }

            session.record_prompt_outcome(signature, "answered", selected_value)
            return {
                "field_key": field_key,
                "selected_value": selected_value,
            }

        if signature is None or force:
            return await _prompt()

        # Two callers asking the same question at once (parallel tasks, batched
        # tool calls) must show one dialog: the loser waits here, then sees the
        # recorded answer and reuses it.
        async with session.prompt_lock(signature):
            suppressed = _suppression_result(session, signature, field_key, force)
            if suppressed is not None:
                return suppressed
            return await _prompt()
