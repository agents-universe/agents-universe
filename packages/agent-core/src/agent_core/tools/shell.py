"""Shell tool — restricted Bash execution for git and file operations."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..sandbox import (
    python_guard_env,
    spawn_in_new_session,
    terminate_process_tree,
    validate_command,
)
from .base import Tool, ToolContext

_log = logging.getLogger(__name__)
_TIMEOUT = 30
_MAX_TIMEOUT = 300
_NPM_INSTALL_TIMEOUT = 120
_MAX_OUTPUT = 10_000

# On Windows, use Git Bash so Unix commands (find, grep, etc.) work correctly.
# Searched in order; first match wins. Falls back to cmd.exe if none found.
_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]

def _get_shell() -> list[str] | None:
    """Return [shell, flag] for subprocess, or None to use create_subprocess_shell default."""
    if sys.platform != "win32":
        return None  # Linux/macOS: create_subprocess_shell uses /bin/sh natively
    for candidate in _GIT_BASH_CANDIDATES:
        if Path(candidate).exists():
            return [candidate, "-c"]
    return None  # fall back to cmd.exe

_SHELL_ARGS = _get_shell()


def redact_secrets(text: str, secrets: dict[str, str]) -> str:
    """Replace each resolved secret value with '[REDACTED:<ENV_NAME>]'.

    Exact substring match only. Applied AFTER decode and BEFORE truncation so
    a secret straddling the _MAX_OUTPUT boundary is fully masked. There is no
    minimum-length guard: over-redaction garbles output but never leaks,
    under-redaction is a leak. Base64/escaped variants are out of scope.
    """
    for name, value in secrets.items():
        if value:
            text = text.replace(value, f"[REDACTED:{name}]")
    return text


# Synthetic git identity for stock containers that ship no git config.
# Injected ONLY for the fields the repo (or global/system config) does not
# define — git resolves identity as env vars > config files, so unconditional
# injection would silently replace a repo-local user.name/email.
_GIT_IDENTITY_DEFAULTS = {
    "GIT_AUTHOR_NAME": "Agents Universe",
    "GIT_AUTHOR_EMAIL": "agents-universe@localhost",
    "GIT_COMMITTER_NAME": "Agents Universe",
    "GIT_COMMITTER_EMAIL": "agents-universe@localhost",
}


def _repo_missing_git_identity(cwd: str) -> dict[str, str]:
    """Return the synthetic git identity env for identity fields that are missing.

    Probes ``git config user.name`` / ``user.email`` (merged local + global +
    system). A non-repo directory reports no identity and gets the defaults,
    which is harmless — git commit would fail there for other reasons anyway.
    Returns {} when git itself is unavailable.
    """
    identity: dict[str, str] = {}
    try:
        for key in ("name", "email"):
            proc = subprocess.run(
                ["git", "-C", cwd, "config", f"user.{key}"],
                capture_output=True, text=True, timeout=5,
            )
            configured = proc.returncode == 0 and bool(proc.stdout.strip())
            if not configured:
                env_name = f"GIT_AUTHOR_{key.upper()}"
                identity[env_name] = _GIT_IDENTITY_DEFAULTS[env_name]
                identity[f"GIT_COMMITTER_{key.upper()}"] = _GIT_IDENTITY_DEFAULTS[f"GIT_COMMITTER_{key.upper()}"]
    except Exception:
        return {}  # git missing or broken — leave the environment untouched
    return identity


def _build_env(
    context: ToolContext,
    *,
    extra: dict[str, str] | None = None,
    git_identity: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build subprocess environment with JAVA_HOME/bin on PATH.

    Uses safe_env() to strip credential-like keys (SECRET_KEY, DB URLs,
    etc.) so LLM-generated commands cannot exfiltrate secrets. `extra` is
    merged AFTER the strip — it carries vault-resolved secrets whose plaintext
    never enters the LLM context (injected only into the subprocess env).
    `git_identity` carries only the identity fields the repo does not define.
    """
    env = context.safe_env()
    java_home = env.get("JAVA_HOME", "")
    if java_home:
        java_bin = os.path.join(java_home, "bin")
        path = env.get("PATH", "")
        if java_bin not in path.split(os.pathsep):
            env["PATH"] = java_bin + os.pathsep + path
    # Stock containers ship no git identity, so `git commit` would fail with
    # "Please tell me who you are". Only fill the missing fields — real repo
    # or global config (or explicit env vars) must keep winning.
    if git_identity:
        env.update(git_identity)
    # Arm the sitecustomize file-access guard for any Python this command
    # (or its children) spawns. Inherits through the whole process tree.
    env.update(python_guard_env(context.project_fs_path))
    if extra:
        env.update(extra)
    return env


async def _resolve_env_refs(
    context: ToolContext, env_refs: dict[str, dict]
) -> tuple[dict[str, str] | None, list[str], str | None]:
    """Resolve env_refs (env name -> {ref, scope, environment}) against the vault.

    Returns (resolved, missing, error). scope='user' reads user_tokens only;
    scope='project' reads project_secrets with user_tokens fallback (parity
    with api_request). Fail-fast: any missing ref blocks the command — a
    partial credential-free run would produce misleading results. A missing
    db_session degrades naturally to a missing ref (get_*_optional -> None).
    """
    from ._auth import get_secret_optional, get_token_optional

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for env_name, spec in env_refs.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("ref"), str) or not spec["ref"]:
            return None, [], f"env_refs[{env_name!r}] must be an object with a string 'ref'"
        scope = spec.get("scope", "user")
        if scope not in ("user", "project"):
            return None, [], f"env_refs[{env_name!r}].scope must be 'user' or 'project'"
        if scope == "user":
            value = await get_token_optional(context, spec["ref"])
        else:
            value = await get_secret_optional(context, spec["ref"], environment=spec.get("environment"))
        if value is None:
            missing.append(spec["ref"])
        else:
            resolved[env_name] = value
    if missing:
        return None, missing, None
    return resolved, [], None


# Allowlist of safe commands.  Kept in sync with the sandbox validator
# (sandbox.py _DATA_COMMANDS / _FIRST_POS_DATA / _DATA_VALUE_OPTS) which
# already knows how to path-check these commands' arguments.
_ALLOWED_CMDS = re.compile(
    r"^(git|ls|cat|grep|find|jq|echo|printf|pwd|head|tail|wc|sort|uniq|diff|"
    r"mkdir|cp|mv|touch|stat|file|date|basename|dirname|"
    r"which|whoami|uname|printenv|test|"
    r"sed|awk|gawk|cut|tr|"
    r"npx|npm|node|python3?|java|javac|mvn)"
    r"(?:\s|$)"
)
_WRAPPER_COMMAND = re.compile(r"^\./(?:mvnw|gradlew)(?:\s|$)")

# Blocklist patterns (additional safety net)
_BLOCKED_PATTERNS = re.compile(
    r"(rm\s+-rf|rm\s+-r|sudo|chmod\s+[0-7]*7|curl\s+|wget\s+|pip\s+install|apt-get|yum|"
    # -exec\b alone missed -execdir (no word boundary after "exec")
    # — find -execdir bash -c '...' bypassed the blocklist and the sandbox
    # path checks only cover the find args, not what bash executes.
    r"-exec\b|-execdir\b|-ok\b|-okdir\b|xargs\b|-delete\b)"
)


def _check_allowlist_per_segment(command: str) -> str | None:
    """Validate every command segment in a compound pipeline against the allowlist.

    Splits the command on shell *chaining* separators (; | && || &) using shlex,
    then checks that each segment starts with an allowed command.  Parentheses
    are deliberately NOT treated as separators here: ``find`` uses ``\\(`` and
    ``\\)`` for expression grouping, and shlex in posix mode strips the
    backslash, making them indistinguishable from shell subshell parens.
    Splitting on them would break every ``find \\( ... \\)`` command.

    Leading VAR=value assignments are skipped to reach the actual command
    token.  Returns None if all segments pass, else the first offending token.
    """
    # Only chain operators, NOT ( ) — see docstring.
    _chain_seps = frozenset({"&&", "||", ";;", ";", "|", "&"})
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None  # malformed - let validate_command report the parse error

    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _chain_seps:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)

    for seg in segments:
        idx = 0
        # Skip leading VAR=value assignments (e.g. FOO=bar cmd ...)
        while idx < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[idx]):
            idx += 1
        if idx >= len(seg):
            continue
        cmd_token = seg[idx]
        # Strip leading ./ for wrapper commands
        check = _ALLOWED_CMDS.match(cmd_token) or _WRAPPER_COMMAND.match(cmd_token)
        if not check:
            return cmd_token
    return None


def _strip_env_prefix(command: str) -> str:
    """Leading VAR=value assignments (FOO=bar npm ...) before the command word."""
    seg = command.split()
    idx = 0
    while idx < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[idx]):
        idx += 1
    return " ".join(seg[idx:]) if idx else command


def _required_node_bins(pkg_dir: Path, command: str) -> list[str]:
    """Return missing declared dependencies and their npm bin shims."""
    try:
        import json
        package = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(package, dict):
        return ["valid package.json"]
    declared = {}
    for key in ("dependencies", "devDependencies"):
        values = package.get(key, {})
        if isinstance(values, dict):
            declared.update(values)
    requirements: list[tuple[str, Path, str]] = []
    needs_playwright = "@playwright/test" in declared or "playwright" in command
    needs_typescript = "typescript" in declared or re.search(r"\btsc\b|typecheck", command)
    needs_eslint = "eslint" in declared or re.search(r"\beslint\b|lint", command)
    if needs_playwright:
        requirements.append(("@playwright/test", pkg_dir / "node_modules/@playwright/test", "playwright"))
    if needs_typescript:
        requirements.append(("typescript", pkg_dir / "node_modules/typescript", "tsc"))
    if needs_eslint:
        requirements.append(("eslint", pkg_dir / "node_modules/eslint", "eslint"))
    missing = []
    for name, module_path, bin_name in requirements:
        if not module_path.exists():
            missing.append(name)
        bin_dir = pkg_dir / "node_modules/.bin"
        # Windows npm creates .cmd shims; accept either form.
        if not (bin_dir / bin_name).exists() and not (bin_dir / (bin_name + ".cmd")).exists():
            missing.append(f".bin/{bin_name}")
    return missing


async def _rm_dir(path: str) -> str | None:
    """Remove a directory tree in a background thread and report failures."""
    import shutil
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, shutil.rmtree, path)
    except OSError as e:
        _log.warning("Failed to remove %s: %s", path, e)
        return f"Failed to clean up node_modules before npm install: {e}"
    return None


async def ensure_node_deps(
    command: str, cwd: str, project_root: str, env: dict[str, str],
    *, install_timeout: int = _NPM_INSTALL_TIMEOUT,
) -> str | None:
    """Install and validate local Node dependencies before running a command.

    Shared by the shell tool and the API's script executor (Playwright runs).
    npm's bin links are intentionally left enabled in Linux containers. The
    generated Playwright tests invoke the local ``playwright`` and ``tsc``
    shims, so checking only for the node_modules directory is insufficient.
    """
    if not re.match(r"^(npx\b|npm\s+(?:run\b|test\b|install\b|i\b))", _strip_env_prefix(command)):
        return None

    cwd_path = Path(cwd)
    # the upward search must stop at the project root. A
    # project workspace nested inside a monorepo (package.json in an
    # ancestor outside the workspace) previously let npm install - and
    # the node_modules rmtree below - run OUTSIDE the project sandbox.
    root = Path(project_root).resolve()
    search = cwd_path
    pkg_dir: Path | None = None
    for _ in range(5):
        if (search / "package.json").is_file():
            pkg_dir = search
            break
        if search == root or not search.is_relative_to(root):
            break
        parent = search.parent
        if parent == search:
            break
        search = parent
    if pkg_dir is None and "playwright" in command:
        tests_candidate = cwd_path / "tests"
        if (tests_candidate / "package.json").is_file():
            pkg_dir = tests_candidate
    if pkg_dir is None:
        return None

    required = _required_node_bins(pkg_dir, command)
    nm = pkg_dir / "node_modules"
    if nm.exists() and os.access(str(nm), os.W_OK) and not required:
        return None
    if nm.exists() and not os.access(str(nm), os.W_OK):
        _log.info("Removing unwritable node_modules in %s", pkg_dir)
        cleanup_error = await _rm_dir(str(nm))
        if cleanup_error:
            return cleanup_error

    npm_cache = "/tmp/npm-cache"
    if sys.platform != "win32":
        try:
            os.makedirs(npm_cache, exist_ok=True)
        except OSError as exc:
            return f"Failed to prepare npm cache: {exc}"
        prefix = f"npm_config_cache={npm_cache} "
    else:
        prefix = ""
    lockfile = pkg_dir / "package-lock.json"
    # npm ci rejects package.json changes that have not been reflected in
    # package-lock.json. The test generator may add scripts/dependencies to
    # an existing scaffold, so use npm install until the lock catches up.
    use_ci = lockfile.is_file() and lockfile.stat().st_mtime >= (pkg_dir / "package.json").stat().st_mtime
    install_cmd = f"{prefix}npm {'ci' if use_ci else 'install'} --prefer-offline"
    _log.info("Installing Node.js deps in %s", pkg_dir)

    async def _run_install(cmd: str) -> tuple[int | None, str]:
        try:
            if _SHELL_ARGS:
                proc = await asyncio.create_subprocess_exec(
                    *_SHELL_ARGS, cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=str(pkg_dir), env=env,
                    **spawn_in_new_session(),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=str(pkg_dir), env=env,
                )
        except OSError as exc:
            _log.warning("Failed to start npm dependency install in %s: %s", pkg_dir, exc)
            return None, f"Failed to start npm dependency install: {exc}"
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=install_timeout)
        except asyncio.TimeoutError:
            terminate_process_tree(proc)
            await proc.wait()
            _log.warning("npm dependency install timed out in %s", pkg_dir)
            return -1, f"npm dependency install timed out after {install_timeout}s"
        except asyncio.CancelledError:
            terminate_process_tree(proc)
            await proc.wait()
            raise
        return proc.returncode, stderr.decode(errors="replace")[:_MAX_OUTPUT]

    returncode, stderr_text = await _run_install(install_cmd)
    if returncode in (None, -1):
        return stderr_text
    if returncode != 0:
        # ENOTEMPTY means a previous install left a corrupt node_modules.
        # Wipe it and retry once before giving up.
        if "ENOTEMPTY" in stderr_text and nm.exists():
            _log.warning("npm ENOTEMPTY in %s - removing node_modules and retrying", pkg_dir)
            cleanup_error = await _rm_dir(str(nm))
            if cleanup_error:
                return cleanup_error
            returncode, stderr_text = await _run_install(f"{prefix}npm install")
            if returncode in (None, -1):
                return stderr_text
        if returncode != 0:
            _log.warning("npm dependency install failed (exit=%d) in %s: %s", returncode, pkg_dir, stderr_text[:500])
            return f"npm dependency install failed (exit code {returncode}): {stderr_text[:1000]}"

    missing = _required_node_bins(pkg_dir, command)
    if missing:
        return "npm dependency install completed but required local binaries are missing: " + ", ".join(missing)
    return None


class ShellTool(Tool):
    name = "shell"
    prompt_hint = (
        "Allowed commands: git, ls, cat, grep, find, jq, sed, awk, cut, tr, "
        "echo, printf, pwd, head, tail, wc, sort, uniq, diff, mkdir, cp, mv, "
        "touch, stat, file, date, basename, dirname, which, whoami, uname, "
        "printenv, test, npx, npm, node, python, java, javac, mvn, ./mvnw, ./gradlew. "
        "Set environment variables with VAR=value prefixes. "
        "Never use `cd` - use the `cwd` parameter instead. `git clone` is blocked - "
        "use the `git_repo` tool to clone repositories. "
        "ALL paths must be relative to the project root - absolute paths, ~, and ../ "
        "are rejected. If a command is rejected, switch to a dedicated tool instead of "
        "rephrasing."
    )
    description = (
        "Run shell commands (bash) in a restricted environment. "
        "Never use `cd` - pass the working directory via the `cwd` parameter instead. "
        "`git clone` is blocked in shell - clone repositories with the `git_repo` tool. "
        "Allowed: git, ls, cat, grep, find, jq, echo, printf, pwd, head, tail, wc, "
        "sort, uniq, diff, mkdir, cp, mv, touch, stat, file, date, basename, dirname, "
        "which, whoami, uname, printenv, test, sed, awk, gawk, cut, tr, "
        "npx, npm, node, python, java, javac, mvn, ./mvnw, ./gradlew. "
        "Every command in a compound pipeline (separated by ; | && ||) must be in the allowlist. "
        "All paths must stay inside the current project: absolute paths, '~', and '..' segments are rejected "
        "for file arguments, and redirection targets are validated the same way (the system temp dir is also allowed). "
        "Data arguments are NOT path-checked: commit messages (git -m), grep patterns, python -c code, and similar "
        "option values may freely contain '/', '~', or '..'. "
        "git supports common subcommands (status/diff/log/show/add/commit/push/pull/fetch/checkout/switch/restore/"
        "branch/tag/stash/blame/reset/rm/mv/revert/rebase/clean/cherry-pick/apply/am/config/submodule/worktree/remote "
        "and more); --git-dir/--work-tree/--exec-path are not allowed, git config is repo-local only (no --global/--system), "
        "and git clone is blocked. A default git identity is injected so commits work out of the box. "
        "python runs under a runtime file-access guard: scripts and 'python -m pytest' work normally, "
        "but reading or writing files outside the project is denied; -S/-I/-E flags are rejected. "
        "node does not support -e/--eval/-p/--print; write a script file and run it instead. "
        "Command substitution ($(...) or backticks) is not allowed — compute values in a first command, then use them. "
        "Blocked: rm -rf, sudo, curl, wget, pip install, find -delete. "
        "Secrets injected via env_refs are resolved server-side and never appear in returned output; "
        "the command fails without running if any referenced secret is missing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "cwd": {"type": "string", "description": "Working directory (relative to project root)"},
            "timeout_seconds": {
                "type": "integer", "default": 30, "minimum": 1, "maximum": 300,
                "description": "Command timeout in seconds (capped at 300)",
            },
            "env_refs": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "description": "Vault service_key to resolve (e.g. 'qa:login:password')"},
                        "scope": {
                            "type": "string", "enum": ["user", "project"], "default": "user",
                            "description": "'user' = user key vault (user_tokens); 'project' = project secrets with user_tokens fallback",
                        },
                        "environment": {
                            "type": "string",
                            "description": "Environment qualifier, used with scope='project'",
                        },
                    },
                    "required": ["ref"],
                },
                "description": (
                    "Resolve vault secrets into subprocess env vars, e.g. "
                    '{"APP_PASSWORD": {"scope": "user", "ref": "qa:login:password"}}. '
                    "Resolved plaintext values are redacted from all returned output. "
                    "The command fails without running if any ref is missing."
                ),
            },
        },
        "required": ["command"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        command = params["command"].strip()

        # Security checks
        if _BLOCKED_PATTERNS.search(command):
            return {"error": "Command blocked for safety: contains disallowed operation"}

        # Every command segment in a compound pipeline must be in the allowlist.
        bad_token = _check_allowlist_per_segment(command)
        if bad_token is not None:
            return {"error": f"Command not in allowlist. First unallowed token: {bad_token!r}"}

        timeout_value = params.get("timeout_seconds", _TIMEOUT)
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, int):
            return {"error": "timeout_seconds must be an integer between 1 and 300"}
        if not 1 <= timeout_value <= _MAX_TIMEOUT:
            return {"error": f"timeout_seconds must be between 1 and {_MAX_TIMEOUT}"}

        # Resolve working directory (must stay inside the project)
        cwd = context.project_fs_path
        if params.get("cwd"):
            base = Path(context.project_fs_path).resolve()
            cwd_path = (base / params["cwd"]).resolve()
            if not cwd_path.is_relative_to(base):
                return {"error": "Working directory must be within project scope"}
            cwd = str(cwd_path)

        # Token-level path/command validation against cross-project escape
        # ('..' segments, absolute paths, redirects, git -C/--git-dir, etc.).
        reject_reason = validate_command(
            command, cwd=Path(cwd), project_root=Path(context.project_fs_path)
        )
        if reject_reason:
            return {"error": f"Command blocked for safety: {reject_reason}"}

        # Resolve vault secrets into the subprocess env BEFORE any side effects
        # (npm install) — a command with missing refs must never run, even
        # partially. Resolved values never enter the LLM context.
        env_refs = params.get("env_refs")
        resolved_env: dict[str, str] = {}
        if env_refs:
            resolved_env, missing, ref_error = await _resolve_env_refs(context, env_refs)
            if ref_error:
                return {"error": ref_error}
            if missing:
                return {
                    "error": (
                        "env_refs unresolved — missing vault secrets: "
                        f"{missing}. Ask the user to save them (user_confirm "
                        "secret mode or secret_vault save), then retry the command."
                    ),
                    "missing_service_keys": missing,
                }

        # Auto-install Node.js dependencies when running npx/npm commands
        # in a directory that has package.json but no usable node_modules.
        install_error = await self._ensure_node_deps(command, cwd, context)
        if install_error:
            return {"error": redact_secrets(install_error, resolved_env)}

        # Inject npm cache env to avoid /.npm permission errors in containers.
        try:
            command = self._inject_npm_cache_env(command, cwd)
        except OSError as exc:
            return {"error": f"Failed to prepare npm cache: {exc}"}

        # Only fill git identity fields the repo does not configure (never
        # override a repo-local or global git identity).
        git_identity = await asyncio.to_thread(_repo_missing_git_identity, cwd)
        env = _build_env(context, extra=resolved_env or None, git_identity=git_identity)
        try:
            if _SHELL_ARGS:
                proc = await asyncio.create_subprocess_exec(
                    *_SHELL_ARGS, command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    # Own session on POSIX so a backgrounded grandchild dies
                    # with the tree on timeout/abort.
                    **spawn_in_new_session(),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
        except OSError as exc:
            return {"error": f"Failed to start shell command: {exc}"}
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_value)
        except asyncio.TimeoutError:
            terminate_process_tree(proc)
            await proc.wait()
            return {"error": f"Command timed out after {timeout_value}s"}
        except asyncio.CancelledError:
            # Task cancelled (user abort / session close): the child must not
            # keep running with side effects (git push, npm install, writes).
            terminate_process_tree(proc)
            await proc.wait()
            raise

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        # Redact BEFORE truncation so a secret straddling the _MAX_OUTPUT
        # boundary is fully masked, then truncate.
        stdout_str = redact_secrets(stdout_text, resolved_env)[:_MAX_OUTPUT]
        stderr_str = redact_secrets(stderr_text, resolved_env)[:_MAX_OUTPUT]
        if proc.returncode != 0:
            _log.warning("shell command failed (exit=%d): %r\nstderr: %s", proc.returncode, command, stderr_str[:500])
        return {
            "stdout": stdout_str,
            "stderr": stderr_str,
            "exit_code": proc.returncode,
        }

    # Module-level implementations shared with the API's script executor;
    # kept as attributes so existing callers (and tests) keep working.
    _strip_env_prefix = staticmethod(_strip_env_prefix)
    _required_node_bins = staticmethod(_required_node_bins)
    _rm_dir = staticmethod(_rm_dir)

    def _inject_npm_cache_env(self, command: str, cwd: str) -> str:
        """Prefix npm/npx commands with a writable cache directory.

        Linux containers support symlinks, so npm creates local package binaries in
        ``node_modules/.bin`` without disabling bin links.
        """
        if not re.match(r"^(npm|npx)\b", self._strip_env_prefix(command)):
            return command
        if "npm_config_cache=" not in command and "--cache" not in command:
            if sys.platform != "win32":
                cache_dir = "/tmp/npm-cache"
                os.makedirs(cache_dir, exist_ok=True)
                command = f"npm_config_cache={cache_dir} {command}"
        return command

    async def _ensure_node_deps(self, command: str, cwd: str, context: ToolContext) -> str | None:
        """Delegate to the module-level ensure_node_deps (shared with the API's
        script executor) - the env is built exactly like the command's own."""
        return await ensure_node_deps(command, cwd, context.project_fs_path, _build_env(context))
