"""Tests for DAG-aware task dependency scheduling in agent.py."""
from __future__ import annotations

import json

import pytest

from agent_core.agent import Agent


def _make_tasks(specs: list[tuple[str, list[str]]]) -> list[dict]:
    """Build a task list from (id, depends_on) pairs."""
    return [
        {
            "id": tid,
            "title": f"Task {tid}",
            "tools_needed": [],
            "depends_on": deps,
            "estimated_complexity": "low",
        }
        for tid, deps in specs
    ]


# ── _build_dependency_graph ─────────────────────────────────────────────


def test_graph_independent_tasks():
    """Three tasks with no dependencies: all in initial ready set."""
    tasks = _make_tasks([("a", []), ("b", []), ("c", [])])
    deps, dependents = Agent._build_dependency_graph(tasks)
    assert deps == {"a": set(), "b": set(), "c": set()}
    assert dependents == {"a": set(), "b": set(), "c": set()}


def test_graph_chain():
    """Linear chain: a -> b -> c."""
    tasks = _make_tasks([("a", []), ("b", ["a"]), ("c", ["b"])])
    deps, dependents = Agent._build_dependency_graph(tasks)
    assert deps == {"a": set(), "b": {"a"}, "c": {"b"}}
    assert dependents == {"a": {"b"}, "b": {"c"}, "c": set()}


def test_graph_diamond():
    """Diamond: a -> {b, c} -> d."""
    tasks = _make_tasks([
        ("a", []),
        ("b", ["a"]),
        ("c", ["a"]),
        ("d", ["b", "c"]),
    ])
    deps, dependents = Agent._build_dependency_graph(tasks)
    assert deps == {"a": set(), "b": {"a"}, "c": {"a"}, "d": {"b", "c"}}
    assert dependents == {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}


def test_graph_unknown_dep_dropped():
    """Unknown dep IDs are silently dropped, not stored."""
    tasks = _make_tasks([("a", []), ("b", ["a", "nonexistent"])])
    deps, _ = Agent._build_dependency_graph(tasks)
    assert deps == {"a": set(), "b": {"a"}}


def test_graph_cycle_detected():
    """Cycle a -> b -> a raises ValueError."""
    tasks = _make_tasks([("a", ["b"]), ("b", ["a"])])
    with pytest.raises(ValueError, match="Cycle detected"):
        Agent._build_dependency_graph(tasks)


def test_graph_self_cycle_detected():
    """Self-referencing task is a cycle."""
    tasks = _make_tasks([("a", ["a"])])
    with pytest.raises(ValueError, match="Cycle detected"):
        Agent._build_dependency_graph(tasks)


# ── _normalize_task_plan preserves depends_on ───────────────────────────


def test_normalize_remaps_depends_on():
    """Normalized tasks should have depends_on IDs remapped to new UUIDs."""
    tasks = _make_tasks([("t1", []), ("t2", ["t1"])])
    normalized = Agent._normalize_task_plan(tasks)
    assert len(normalized) == 2
    new_id_1 = normalized[0]["id"]
    new_id_2 = normalized[1]["id"]
    assert new_id_1 != "t1"
    assert new_id_2 != "t2"
    assert normalized[1]["depends_on"] == [new_id_1]


def test_normalize_preserves_no_deps():
    """Tasks with empty depends_on stay empty after normalization."""
    tasks = _make_tasks([("t1", []), ("t2", [])])
    normalized = Agent._normalize_task_plan(tasks)
    assert normalized[0]["depends_on"] == []
    assert normalized[1]["depends_on"] == []


def test_normalize_coerces_string_depends_on():
    """An LLM-stringified depends_on ("t1,t2") must split into a real list —
    otherwise the DAG iterates characters and silently drops every dependency,
    letting the task run before its prerequisites."""
    tasks = _make_tasks([("t1", []), ("t2", ["t1"])])
    # Stringify t2's deps the way an LLM tool-call argument might.
    tasks[1]["depends_on"] = "t1"
    normalized = Agent._normalize_task_plan(tasks)
    new_id_1 = normalized[0]["id"]
    assert normalized[1]["depends_on"] == [new_id_1]


def test_graph_coerces_string_depends_on():
    """_build_dependency_graph must survive a raw string depends_on too."""
    tasks = _make_tasks([("a", []), ("b", ["a"])])
    tasks[1]["depends_on"] = "a"
    deps, dependents = Agent._build_dependency_graph(tasks)
    # b depends on a → not initially ready.
    assert "a" in deps["b"]
    assert "b" in dependents["a"]


# ── Existing tests still pass (regression) ──────────────────────────────
# These are from test_agent_task_messages.py - verifying they still work
# with the refactored code.


def test_task_messages_acknowledge_plan_tool_call_before_task_prompt():
    from agent_core.providers.base import Message

    plan_call = Message(
        role="assistant",
        content="",
        tool_calls=[{
            "id": "call_plan",
            "type": "function",
            "function": {
                "name": "plan_task",
                "arguments": json.dumps({"goal": "Test", "tasks": []}),
            },
        }],
    )
    messages = [Message(role="user", content="Implement the feature"), plan_call]
    task_messages = Agent._build_task_messages(messages, "call_plan", "Update the API")

    assert [m.role for m in task_messages] == ["user", "assistant", "tool", "user"]
    assert task_messages[-2].name == "plan_task"
    assert task_messages[-2].tool_call_id == "call_plan"
    assert json.loads(task_messages[-2].content)["status"] == "accepted"
    assert task_messages[-1].content == "Execute this task: Update the API"
