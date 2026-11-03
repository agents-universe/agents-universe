"""End-to-end: owner secret injection + the per-run tool bridge in real runs.

Every test here goes through ``execute_script`` with REAL sandboxed
subprocesses (python under the audit-hook guard, bash under the blocked-
command checks) — the injection point, the redaction path and the bridge
wire are exactly what production runs use.

Leak rules under test (CLAUDE.md token security): a decrypted secret or the
bridge PIN may exist only in the child's env; stdout_log/stderr_log/result_json
must show [REDACTED] (or a length), never the plaintext, and failure messages
may name the service_key but never a value.
"""
from __future__ import annotations

from sqlalchemy import select

from api.config import get_settings
from api.models.project_secret import ProjectSecret
from api.models.script import AutomationScript, ScriptRun
from api.models.user import UserToken
from api.routers import scripts as scripts_router
from api.services.token_vault import encrypt, encrypt_project_secret


async def _add_project_secret(db, project_id: str, service_key: str, value: str, environment=None):
    db.add(
        ProjectSecret(
            project_id=project_id,
            service_key=service_key,
            environment=environment,
            secret_name="default",
            encrypted_value=encrypt_project_secret(value, project_id),
            key_hint="h",
            created_by="test-user",
        )
    )
    await db.commit()


async def _add_user_token(db, user_id: str, service_key: str, value: str):
    # Upsert: the suite shares one DB and (user_id, service_key) is unique —
    # a later test seeding the same key must replace, not collide.
    from sqlalchemy import delete

    await db.execute(
        delete(UserToken).where(
            UserToken.user_id == user_id, UserToken.service_key == service_key
        )
    )
    db.add(
        UserToken(
            user_id=user_id,
            service_key=service_key,
            encrypted_value=encrypt(value, user_id),
            key_hint="h",
        )
    )
    await db.commit()


async def _make_script(db, project, content: str, script_type: str = "python", **decls) -> AutomationScript:
    script = AutomationScript(
        project_id=str(project.project_id),
        name="decl-test",
        script_type=script_type,
        content=content,
        created_by="test-user",
        **decls,
    )
    db.add(script)
    await db.commit()
    await db.refresh(script)
    return script


async def _run(
    project,
    content: str,
    script_type: str = "python",
    *,
    triggered_by: str = "test-user",
    project_id: str | None = None,
    declared_secrets: list[str] | None = None,
    declared_tools: list[str] | None = None,
) -> ScriptRun:
    """Create a pending run, execute it for real, return the terminal row."""
    from api.database import AsyncSessionLocal
    from api.paths import PROJECTS_ROOT

    async with AsyncSessionLocal() as s:
        script = AutomationScript(
            project_id=str(project.project_id),
            name="run-me",
            script_type=script_type,
            content=content,
        )
        s.add(script)
        await s.commit()
        await s.refresh(script)
        run = ScriptRun(
            script_id=script.script_id,
            triggered_by=triggered_by,
            status="pending",
        )
        s.add(run)
        await s.commit()
        run_id = str(run.run_id)

    await scripts_router._execute_script(
        run_id, content, script_type, triggered_by,
        str(PROJECTS_ROOT / project.slug),
        project_id=project_id if project_id is not None else str(project.project_id),
        declared_secrets=declared_secrets,
        declared_tools=declared_tools,
    )

    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(ScriptRun).where(ScriptRun.run_id == run_id)
        )).scalar_one()


# ── secret injection ────────────────────────────────────────────────────────


async def test_declared_secret_injected_and_redacted_in_log(client, db, make_project):
    """The owner's vault value reaches the child env, and an echo of it lands
    in the log only as [REDACTED] — never as plaintext, in any stored field."""
    project = await make_project("sec-inject")
    pid = str(project.project_id)
    secret_value = "ghp_this_is_the_plaintext_needle_12345"
    await _add_project_secret(db, pid, "demo_key", secret_value)

    run = await _run(
        project,
        "import os\n"
        "v = os.environ['AGENT_SECRET_DEMO_KEY']\n"
        "print('len', len(v))\n"
        "print('value', v)\n",
        declared_secrets=["demo_key"],
    )
    assert run.status == "completed", run.stderr_log
    out = run.stdout_log or ""
    assert f"len {len(secret_value)}" in out          # the value really arrived
    assert "value [REDACTED]" in out                  # but the log never holds it
    assert secret_value not in (run.stdout_log or "") + (run.stderr_log or "")
    assert secret_value not in (run.result_json or "")


async def test_missing_secret_fails_closed_naming_only_the_key(client, db, make_project):
    project = await make_project("sec-missing")
    run = await _run(
        project,
        "print('should not run')\n",
        declared_secrets=["absent_key"],
    )
    assert run.status == "failed"
    err = run.stderr_log or ""
    assert "absent_key" in err                         # the key name is the diagnosis
    assert "should not run" not in (run.stdout_log or "")  # child never started
    # No ciphertext or value can appear — nothing was decrypted.
    assert "encrypted" not in err.lower() or "decryption failed" in err


async def test_user_token_fallback_when_project_has_no_secret(client, db, make_project):
    """No project_secrets row → _auth.get_secret falls back to the owner's
    user_tokens entry (the per-user vault)."""
    project = await make_project("sec-user-fb")
    user_value = "user-level-token-abcdefgh"
    await _add_user_token(db, "test-user", "git", user_value)

    run = await _run(
        project,
        "import os\nprint('len', len(os.environ['AGENT_SECRET_GIT']))\n",
        declared_secrets=["git"],
    )
    assert run.status == "completed", run.stderr_log
    assert f"len {len(user_value)}" in (run.stdout_log or "")
    assert user_value not in (run.stdout_log or "") + (run.stderr_log or "")


async def test_project_secret_wins_over_user_token(client, db, make_project):
    project = await make_project("sec-priority")
    pid = str(project.project_id)
    project_value = "proj-secret-XX"      # len 14
    user_value = "user-token-YYYYYYYYYY"  # len 21
    await _add_project_secret(db, pid, "git", project_value)
    await _add_user_token(db, "test-user", "git", user_value)

    run = await _run(
        project,
        "import os\nprint('len', len(os.environ['AGENT_SECRET_GIT']))\n",
        declared_secrets=["git"],
    )
    assert run.status == "completed", run.stderr_log
    assert f"len {len(project_value)}" in (run.stdout_log or "")
    assert f"len {len(user_value)}" not in (run.stdout_log or "")


async def test_secret_requires_an_owner(client, db, make_project):
    """triggered_by empty → fail before spawn; a run without an owner must
    not silently decrypt under some ambient identity."""
    project = await make_project("sec-no-owner")
    await _add_project_secret(db, str(project.project_id), "demo_key", "v" * 12)

    run = await _run(
        project,
        "print('nope')\n",
        triggered_by="",
        declared_secrets=["demo_key"],
    )
    assert run.status == "failed"
    assert "owner" in (run.stderr_log or "").lower()
    assert "nope" not in (run.stdout_log or "")


# ── tool bridge ─────────────────────────────────────────────────────────────


async def test_python_script_calls_declared_tool_through_bridge(client, db, make_project):
    project = await make_project("bridge-py")
    run = await _run(
        project,
        "from agent_script_tools import call_tool\n"
        "result = call_tool('filesystem', {'operation': 'list_dir', 'path': '.'})\n"
        "print('bridge_ok', 'error' not in result)\n",
        declared_tools=["filesystem"],
    )
    assert run.status == "completed", run.stderr_log
    assert "bridge_ok True" in (run.stdout_log or "")


async def test_python_script_undeclared_tool_refused(client, db, make_project):
    project = await make_project("bridge-py-deny")
    run = await _run(
        project,
        "from agent_script_tools import call_tool\n"
        "call_tool('web_search', {})\n",
        declared_tools=["filesystem"],
    )
    assert run.status == "failed"
    # The child's stderr streams into the combined log (stdout_log), same as
    # every other python run in this suite.
    err = (run.stderr_log or "") + (run.stdout_log or "")
    assert "not declared" in err                       # the bridge's refusal
    assert "web_search" in err


async def test_bash_script_calls_tool_via_cli(client, db, make_project):
    project = await make_project("bridge-bash")
    run = await _run(
        project,
        "agent-tool call filesystem <<'EOF'\n"
        '{"operation": "list_dir", "path": "."}\n'
        "EOF\n"
        'echo "code=$?"\n',
        script_type="bash",
        declared_tools=["filesystem"],
    )
    assert run.status == "completed", run.stderr_log
    out = run.stdout_log or ""
    assert '"ok": true' in out
    assert "code=0" in out


async def test_bash_cli_exit_codes_for_tool_failure(client, db, make_project):
    """Exit 1 = the bridge answered with a failure (undeclared tool), and the
    JSON error rides on stdout for the script to inspect."""
    project = await make_project("bridge-bash-deny")
    run = await _run(
        project,
        "agent-tool call web_search <<'EOF'\n"
        "{}\n"
        "EOF\n"
        'echo "code=$?"\n',
        script_type="bash",
        declared_tools=["filesystem"],
    )
    out = run.stdout_log or ""
    assert "tool_not_allowed" in out
    assert "code=1" in out
    # The script itself succeeded (it handled the error) — the run is green.
    assert run.status == "completed", run.stderr_log


async def test_bridge_env_absent_without_declarations(client, db, make_project):
    project = await make_project("bridge-none")
    run = await _run(
        project,
        "import os\n"
        "print('bridge', os.environ.get('AGENT_TOOL_BRIDGE'))\n"
        "print('secret', os.environ.get('AGENT_SECRET_ANYTHING'))\n",
    )
    assert run.status == "completed", run.stderr_log
    out = run.stdout_log or ""
    assert "bridge None" in out
    assert "secret None" in out


async def test_bridge_pin_echo_is_redacted(client, db, make_project):
    project = await make_project("bridge-pin")
    run = await _run(
        project,
        "import os\n"
        "print('PIN=' + os.environ['AGENT_TOOL_BRIDGE_PIN'])\n",
        declared_tools=["filesystem"],
    )
    assert run.status == "completed", run.stderr_log
    out = run.stdout_log or ""
    assert "PIN=[REDACTED]" in out
    # No 43-char urlsafe token survives anywhere on the row.
    import re

    assert not re.search(r"PIN=[A-Za-z0-9_-]{40,}", out)


async def test_denied_tool_declaration_fails_at_runtime_gate(client, db, make_project):
    """A row carrying a deny-listed tool (written before the deny list, or by
    a direct DB write) degrades to a smaller allowlist — the call is refused,
    the run itself still executes."""
    project = await make_project("bridge-deny-row")
    run = await _run(
        project,
        "from agent_script_tools import call_tool\n"
        "try:\n"
        "    call_tool('shell', {'command': 'ls'})\n"
        "except Exception as e:\n"
        "    print('refused', type(e).__name__)\n"
        "print('done')\n",
        declared_tools=["shell", "filesystem"],
    )
    assert run.status == "completed", run.stderr_log
    out = run.stdout_log or ""
    assert "refused ToolBridgeError" in out
    assert "done" in out
    assert "shell" not in out or "command" not in out  # no shell output possible


# ── REST declarations surface ───────────────────────────────────────────────


async def test_get_and_patch_script_declarations(client, db, make_project):
    project = await make_project("decl-api")
    pid = str(project.project_id)
    script = await _make_script(db, project, "print('x')\n")

    resp = await client.get(f"/api/scripts/{script.script_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["secrets"] == [] and body["tools"] == []
    assert body["created_by"] == "test-user"

    resp = await client.patch(
        f"/api/scripts/{script.script_id}",
        json={"secrets": ["github_token"], "tools": ["github", "mcp:foo"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["secrets"] == ["github_token"]
    assert resp.json()["tools"] == ["github", "mcp:foo"]

    # Invalid declarations are rejected with a readable message.
    resp = await client.patch(
        f"/api/scripts/{script.script_id}",
        json={"tools": ["shell"]},
    )
    assert resp.status_code == 400
    assert "shell" in resp.json()["detail"]["message"]

    # Partial PATCH leaves the other list intact.
    resp = await client.patch(
        f"/api/scripts/{script.script_id}",
        json={"secrets": ["jira"]},
    )
    assert resp.status_code == 200
    body = (await client.get(f"/api/scripts/{script.script_id}")).json()
    assert body["secrets"] == ["jira"]
    assert body["tools"] == ["github", "mcp:foo"]

    # list_scripts surfaces the declarations too.
    listing = (await client.get(f"/api/projects/{pid}/scripts")).json()
    row = next(r for r in listing if r["script_id"] == str(script.script_id))
    assert row["secrets"] == ["jira"] and row["tools"] == ["github", "mcp:foo"]


async def test_tool_catalog_exposes_only_script_safe_tools(client, db, make_project):
    project = await make_project("catalog")
    pid = str(project.project_id)
    resp = await client.get(f"/api/scripts/tool-catalog", params={"project_id": pid})
    assert resp.status_code == 200
    body = resp.json()
    assert "shell" not in body["tools"] and "plan_task" not in body["tools"]
    assert "filesystem" in body["tools"]
    assert "shell" in body["deny"]
    assert "usage" in body and "agent-tool" in body["usage"]


async def test_create_script_records_created_by(client, db, make_project):
    project = await make_project("create-owner")
    pid = str(project.project_id)
    resp = await client.post(
        f"/api/projects/{pid}/scripts",
        json={"name": "owned", "content": "print('x')\n", "script_type": "python"},
    )
    assert resp.status_code == 200
    script_id = resp.json()["script_id"]
    body = (await client.get(f"/api/scripts/{script_id}")).json()
    assert body["created_by"] == "test-user"


async def test_script_writer_roundtrips_declarations(client, db, make_project):
    """The agent-facing tool validates, persists and returns declarations the
    same way the REST layer does (shared validate_declarations)."""
    from agent_core.tools.base import ToolContext
    from agent_core.tools.script_writer import ScriptWriterTool

    project = await make_project("sw-decl")
    pid = str(project.project_id)
    await _add_project_secret(db, pid, "demo_key", "value-123456")

    from api.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        ctx = ToolContext(
            project_id=pid,
            project_fs_path=str(pid),
            conversation_id="c",
            user_id="test-user",
            db_session=s,
            db_session_factory=AsyncSessionLocal,
            secret_key=get_settings().secret_key,
        )
        ctx.interactive = False
        tool = ScriptWriterTool()

        created = await tool.execute(
            {
                "operation": "create",
                "name": "decl",
                "content": "print('x')\n",
                "secrets": ["demo_key"],
                "tools": ["filesystem"],
            },
            ctx,
        )
        assert created.get("success"), created
        assert created["secrets"] == ["demo_key"]
        assert created["tools"] == ["filesystem"]

        got = await tool.execute({"operation": "get", "script_id": created["script_id"]}, ctx)
        assert got["secrets"] == ["demo_key"] and got["tools"] == ["filesystem"]

        # A deny-listed tool is rejected by the tool itself.
        bad = await tool.execute(
            {"operation": "update", "script_id": created["script_id"], "tools": ["shell"]},
            ctx,
        )
        assert "error" in bad and "shell" in bad["error"]
