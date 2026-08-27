"""Runtime file-access guard, auto-imported by Python at interpreter startup.

This directory is prepended to PYTHONPATH by ``agent_core.sandbox.python_guard_env``
for every Python subprocess spawned through the shell tool (and later the
code_executor). The container image additionally installs a copy of this file
into site-packages (see the Dockerfile) so the guard loads even when a command
overrides PYTHONPATH itself, which would otherwise replace the tool-injected
entry and silently disarm the guard. The guard is armed ONLY when the
``_AGENT_PROJECT_ROOT`` env var is set; without it this module is a no-op so
unrelated Python runs (e.g. the API server itself) are unaffected.

Zero-dependency and self-contained on purpose: it must not import agent_core
(the child interpreter may not have it on sys.path).

Root sets (computed in-process):
  - write roots = the project workspace + the system temp dir (pytest tmp_path,
    pip/npm scratch) + per-user tool caches under $HOME (.cache, .matplotlib,
    .npm, .node-gyp, .semgrep) so npm native builds (node-gyp), plotting
    libraries, and the semgrep CLI keep working. TEMP/TMP/TMPDIR are
    intentionally NOT modified.
  - read roots  = write roots + Python installation dirs (sys.base_prefix,
    sys.prefix, site-packages) so stdlib/3rd-party imports and
    ``python -m pytest`` keep working.

Enforcement is a CPython audit hook (``sys.addaudithook``), which user code
cannot remove. Denials raise PermissionError with an ``[agent-guard]`` prefix
so the calling LLM agent understands the rejection is expected sandbox behavior.
Blocked imports raise ImportError (pytest's optional colorama import tolerates
exactly that, so ``python -m pytest`` keeps working even on Windows).

Residual risks (accepted, defense-in-depth only):
  - Non-Python commands spawned via ``subprocess`` from guarded Python are not
    covered in non-strict mode (strict mode, ``_AGENT_BLOCK_SUBPROCESS=1``,
    denies the ``subprocess.Popen`` call itself — and patches the audit-free
    ``os.posix_spawn*`` plus rejects the ``os.spawn``/``os.fork`` audit events
    — with a narrow fc-list/fc-cache allowlist for matplotlib's font probing;
    ``import subprocess`` stays allowed because matplotlib/pandas need it).
    ``os.fork``/``os.forkpty`` emit the same ``os.fork`` audit event and are
    denied in both modes when called directly: a forked child could
    ``setsid()`` out of the kill tree and keep running. The one admitted
    call site is multiprocessing's own worker fork (``popen_fork._launch``,
    what ``Pool``/``Process`` use), and only outside strict mode — library
    tooling such as ``detect-secrets scan`` parallelizes through it. The
    workers inherit this audit hook and the spawn session, and ``os.exec``
    outside the ``_AGENT_EXEC_ALLOWLIST`` directories stays blocked, so they
    remain file-guarded and reachable by the timeout kill. The allowlist
    itself (the semgrep venv, whose console script execvp's the native
    osemgrep binary) opens no new surface: in non-strict mode those same
    binaries were already spawnable unguarded via ``subprocess.Popen``.
  - ``os.stat``/``os.lstat``/``os.access``/``os.readlink`` emit NO audit events
    in CPython 3.12 (verified on Linux and Windows despite the docs table), so
    pure metadata/existence probing of outside paths is not blocked. File
    content reads still are — every read goes through the hooked ``open``.
  - Node.js has no equivalent runtime hook; node scripts can still read ``../``.
"""
from __future__ import annotations

import os
import sys

_ROOT_ENV = "_AGENT_PROJECT_ROOT"
_BLOCK_SUBPROCESS_ENV = "_AGENT_BLOCK_SUBPROCESS"
_EXEC_ALLOWLIST_ENV = "_AGENT_EXEC_ALLOWLIST"


def _canon(p: str) -> str:
    """normcase(realpath(p)) — inline copy of agent_core.paths.canonical."""
    return os.path.normcase(os.path.realpath(p))


def _load_chained_sitecustomize() -> None:
    """Exec the sitecustomize.py our PYTHONPATH prepend would otherwise shadow.

    python_guard_env() puts this directory first on PYTHONPATH. Without
    chaining, a pre-existing sitecustomize (e.g. a corporate SSL/cert shim in
    site-packages) would silently stop loading for every tool-spawned Python.
    Best-effort: a broken shim must never break interpreter startup.
    """
    own = _canon(os.path.abspath(__file__))
    for entry in sys.path:
        if not entry:
            continue
        try:
            cand = os.path.join(entry, "sitecustomize.py")
            if not os.path.isfile(cand) or _canon(cand) == own:
                continue
            with open(cand, "rb") as fh:
                code = compile(fh.read(), cand, "exec")
            exec(code, {"__name__": "sitecustomize_chained", "__file__": cand})
            return
        except Exception:
            continue


_project_root = os.environ.get(_ROOT_ENV)

if _project_root:
    import site
    import tempfile
    import threading

    def _collect_roots() -> tuple[frozenset, frozenset]:
        write = {_canon(_project_root), _canon(tempfile.gettempdir())}
        read = set(write)
        # Per-user tool caches: node-gyp downloads/builds under ~/.cache
        # (npm native modules spawned via the shell tool), matplotlib keeps
        # its font cache there, npm/node-gyp also use ~/.npm and ~/.node-gyp,
        # and semgrep keeps its settings and first-run state in ~/.semgrep
        # (the CLI writes settings.yml at startup). These are not project
        # data, so write access here does not weaken cross-project isolation.
        home = os.path.expanduser("~")
        if home and home != "~":
            for sub in (".cache", ".matplotlib", ".npm", ".node-gyp", ".semgrep"):
                p = _canon(os.path.join(home, sub))
                write.add(p)
                read.add(p)
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache:
            p = _canon(xdg_cache)
            write.add(p)
            read.add(p)
        # Playwright browser binaries installed via Docker (PLAYWRIGHT_BROWSERS_PATH
        # is set as a Docker ENV). Playwright reads these to launch Chromium.
        pw_browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if pw_browsers:
            try:
                read.add(_canon(pw_browsers))
            except OSError:
                pass
        # This guard's own directory is prepended to PYTHONPATH; the import
        # machinery stats it on every post-hook import, so it must be readable.
        read.add(_canon(os.path.dirname(os.path.abspath(__file__))))
        for cand in (sys.base_prefix, sys.prefix):
            try:
                read.add(_canon(cand))
            except Exception:
                pass
        candidates: list[str] = []
        try:
            candidates.extend(site.getsitepackages())
        except Exception:
            pass  # some venvs raise for virtualenv-created interpreters
        try:
            candidates.append(site.getusersitepackages())
        except Exception:
            pass
        for cand in candidates:
            try:
                read.add(_canon(cand))
            except Exception:
                pass
        return frozenset(write), frozenset(read)

    _WRITE_ROOTS, _READ_ROOTS = _collect_roots()

    # Never write .pyc bytecode: site-packages are read roots only, so a
    # bytecode write there would trip the guard itself.
    sys.dont_write_bytecode = True

    # NOTE: ctypes/_ctypes are deliberately NOT blocked — pandas imports
    # ctypes at module level (errors module), so blocking it broke pandas in
    # both the shell and code_executor paths. The tools' text-level regexes
    # reject user code that names ctypes explicitly; the residual dynamic
    # import is accepted defense-in-depth risk (same tier as subprocess).
    _BLOCKED_IMPORTS = frozenset({"pty"})
    _STRICT = os.environ.get(_BLOCK_SUBPROCESS_ENV) == "1"
    # Network policy for sandboxed Python (code_executor's SANDBOX_NETWORK
    # config, see .env.example). Default "all": no interception — the agent
    # needs full Playwright functionality (downloads, screen recording, ...).
    # "localhost": only loopback connections (Playwright talks to its browser
    # subprocess via WebSocket on a loopback port). "none": all sockets
    # blocked — the old strict behavior that kept a sandboxed script from
    # reaching Redis/MSSQL/API on the compose network.
    _NETWORK_MODE = os.environ.get("_AGENT_NETWORK_MODE", "all")
    _LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})
    # Directories whose binaries a guarded Python may os.exec into (see
    # sandbox.py _EXEC_ALLOW_DIRS - the semgrep console script execvp's the
    # native osemgrep binary out of its venv to replace its own process).
    # Canonicalized so symlinked entries resolve to their real targets.
    _exec_allow = set()
    for _entry in os.environ.get(_EXEC_ALLOWLIST_ENV, "").split(os.pathsep):
        if _entry:
            try:
                _exec_allow.add(_canon(_entry))
            except Exception:
                continue  # unresolvable entry - fail closed
    _EXEC_ALLOW_DIRS = frozenset(_exec_allow)
    # NOTE: strict mode deliberately does NOT block `import subprocess`.
    # matplotlib and pandas import it internally (font probing, IO helpers);
    # blocking the import broke both. The guard instead denies the Popen CALL
    # below — the actual escape path — and the tool's text-level regex already
    # blocks user code that names subprocess explicitly.

    if _STRICT:
        # os.posix_spawn/os.posix_spawnp emit NO audit event (verified in
        # CPython 3.12) — they would bypass the Popen block below and run an
        # unguarded child (cat /etc/passwd, any binary). Patch them at
        # startup so the denial matches the Popen one; the os.spawn* family
        # is covered by the "os.spawn" audit event in _dispatch.
        def _deny_posix_spawn(*_args, **_kwargs):
            raise PermissionError(
                "[agent-guard] os.posix_spawn is blocked in the strict "
                "sandbox; use the shell tool for external commands"
            )
        for _spawn_name in ("posix_spawn", "posix_spawnp"):
            if hasattr(os, _spawn_name):
                setattr(os, _spawn_name, _deny_posix_spawn)

    # Reentrancy guard: canonicalizing a path may itself trigger audited
    # syscalls (e.g. realpath's lstat on Linux). Those inner events must not
    # be re-checked — the parent components of a legitimate in-project path
    # are outside the read roots and would otherwise be denied.
    _local = threading.local()

    _O_WRITE_MASK = 0
    for _flag in ("O_WRONLY", "O_RDWR", "O_TRUNC", "O_CREAT", "O_APPEND"):
        _O_WRITE_MASK |= getattr(os, _flag, 0)

    # Write-class events: every path argument must stay in the write roots.
    _WRITE_ONE_PATH = frozenset({
        "os.remove", "os.unlink", "os.mkdir", "os.rmdir", "os.chmod", "os.utime",
    })
    _WRITE_TWO_PATHS = frozenset({
        "os.rename", "os.replace", "os.link", "os.symlink", "shutil.copyfile",
    })
    # Read-class events: the path argument must stay in the read roots.
    # NOTE: os.stat/os.access/os.readlink currently emit no audit events in
    # CPython 3.12; they are listed anyway in case a future version adds them.
    _READ_ONE_PATH = frozenset({
        "os.listdir", "os.scandir", "os.stat", "os.access", "os.chdir",
        "os.readlink", "os.walk",
    })

    def _deny(action: str, path) -> None:
        raise PermissionError(
            f"[agent-guard] {action} access outside project sandbox: {path}"
        )

    def _is_within(roots: frozenset, canon_path: str) -> bool:
        for root in roots:
            if canon_path == root or canon_path.startswith(root + os.sep):
                return True
        return False

    def _check(action: str, path, roots: frozenset) -> None:
        # Defensive: only str/bytes path arguments are checked; anything else
        # (fd ints, None) is skipped so exotic-but-legitimate calls keep working.
        if isinstance(path, bytes):
            try:
                path = os.fsdecode(path)
            except Exception:
                return
        if not isinstance(path, str):
            return
        # The Windows null device (any spelling) is not a real file; tools like
        # pytest open it for write. Realpath would map it outside every root.
        base = path.rstrip("\\/").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        if path == os.devnull or base.lower() == "nul":
            return
        try:
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            canon = _canon(path)
        except Exception:
            _deny(action, path)  # resolution failed — fail closed
        if not _is_within(roots, canon):
            _deny(action, path)

    def _audit_hook(event: str, args: tuple) -> None:
        if getattr(_local, "busy", False):
            return
        _local.busy = True
        try:
            _dispatch(event, args)
        finally:
            _local.busy = False

    def _from_socketpair(start_frame) -> bool:
        # CPython's pure-Python socketpair fallback (Windows and any platform
        # without native AF_UNIX socketpair) builds its two endpoints through
        # ordinary socket.socket()/connect calls, so its audit events land in
        # the socket block below. Exempting them is safe: the pair's endpoints
        # connect only to each other (a loopback listener the process itself
        # owns), so no network escape opens — and a direct
        # socket.socket(AF_UNIX, ...) by user code has no socketpair frame on
        # the stack and stays blocked. asyncio's event loop creates its
        # self-pipe through socket.socketpair(), so this keeps `asyncio.run()`
        # working inside the sandbox.
        frame = start_frame
        for _ in range(8):
            if frame is None:
                return False
            if (
                frame.f_code.co_name in ("_fallback_socketpair", "socketpair")
                and frame.f_globals.get("__name__") == "socket"
            ):
                return True
            frame = frame.f_back
        return False

    def _from_multiprocessing_fork(start_frame) -> bool:
        # multiprocessing's fork start method calls os.fork() from
        # popen_fork.Popen._launch — the one call site library tooling
        # (detect-secrets scan) parallelizes through. A direct os.fork() in
        # user code (daemon, fork bomb) has no such frame and stays denied.
        frame = start_frame
        for _ in range(8):
            if frame is None:
                return False
            if (
                frame.f_code.co_name == "_launch"
                and frame.f_globals.get("__name__") == "multiprocessing.popen_fork"
            ):
                return True
            frame = frame.f_back
        return False

    def _dispatch(event: str, args: tuple) -> None:
        if event == "open":
            if not args:
                return
            padded = list(args) + [None, None]
            path, mode, flags = padded[0], padded[1], padded[2]
            if path is None or isinstance(path, int):
                return  # fd-based open — nothing to check
            writing = False
            if isinstance(flags, int) and flags & _O_WRITE_MASK:
                writing = True
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                writing = True
            if writing:
                _check("write", path, _WRITE_ROOTS)
            else:
                _check("read", path, _READ_ROOTS)
            return

        if event in ("os.system", "os.exec", "os.spawn"):
            # os.exec into an _EXEC_ALLOW_DIRS directory is the one admission
            # (non-strict only): the semgrep console script replaces itself
            # with the native osemgrep binary from its venv via os.execvp, and
            # the audit event fires before the process image is swapped. The
            # exec'd process keeps the shell tool's session, so the timeout
            # kill still reaches it; every other target - including
            # PATH-resolved bare names, which carry no directory to check -
            # stays denied. In strict mode the allowlist is ignored: an exec
            # there would swap the guarded interpreter for an unguarded
            # binary past the Popen block.
            if (
                event == "os.exec"
                and not _STRICT
                and args
                and isinstance(args[0], str)
            ):
                try:
                    if _is_within(_EXEC_ALLOW_DIRS, _canon(args[0])):
                        return
                except Exception:
                    pass  # unresolvable target - fail closed below
            raise PermissionError(
                f"[agent-guard] {event} is blocked in the agent sandbox; "
                "use the shell tool instead"
            )
        if event == "os.fork":
            # multiprocessing.Pool/Process fork their workers from
            # popen_fork.Popen._launch — admit exactly that call site, and
            # only outside strict mode, so library tooling that parallelizes
            # (detect-secrets scan) keeps working. The workers inherit this
            # audit hook (file guard stays armed inside them) and the shell
            # tool's session (killpg reaches them on timeout); os.exec stays
            # blocked, so they cannot swap to an unguarded binary. Direct
            # os.fork() calls (daemons, fork bombs) stay blocked in both
            # modes.
            if not _STRICT and _from_multiprocessing_fork(sys._getframe(2)):
                return
            raise PermissionError(
                f"[agent-guard] {event} is blocked in the agent sandbox; "
                "use the shell tool instead"
            )

        if event == "subprocess.Popen" and _STRICT:
            argv = args[0] if args else None
            # Narrow allowlist: matplotlib probes system fonts via fc-list /
            # fc-cache on Linux. Everything else denies the spawn — this is
            # the call site that would escape the file guard.
            if not (
                isinstance(argv, (list, tuple))
                and argv
                and isinstance(argv[0], str)
                and argv[0].replace("\\", "/").rsplit("/", 1)[-1]
                in ("fc-list", "fc-cache")
            ):
                raise PermissionError(
                    "[agent-guard] subprocess.Popen is blocked in the strict "
                    "sandbox; use the shell tool for external commands"
                )
            return

        if event in (
            "socket.socket", "socket.__new__", "socket.connect", "socket.getaddrinfo",
        ):
            # Network policy follows _AGENT_NETWORK_MODE ("all" by default):
            # "all" intercepts nothing; "localhost" allows loopback only
            # (Playwright's WebSocket to its browser subprocess); "none"
            # blocks everything, so a sandboxed script cannot read sessions
            # or secrets from Redis/MSSQL/API on the compose network. The
            # socket constructor event is named socket.__new__ on Python 3.12
            # and was renamed to socket.socket in 3.13 - both are listed so
            # every supported interpreter is covered (covers http.client,
            # urllib, requests, raw ssl); connect/getaddrinfo close the
            # direct-call gaps. socketpair's own internal socket()/connect
            # calls are exempted (see _from_socketpair) so asyncio's self-pipe
            # keeps working in every mode; getaddrinfo is never used by
            # socketpair and stays gated by the mode below.
            if event != "socket.getaddrinfo" and _from_socketpair(sys._getframe(2)):
                return
            if _NETWORK_MODE == "all":
                return
            if _NETWORK_MODE == "localhost":
                if event in ("socket.socket", "socket.__new__"):
                    return
                if event == "socket.connect" and len(args) >= 2:
                    addr = args[1]
                    if isinstance(addr, (tuple, list)) and addr:
                        host = addr[0]
                        if isinstance(host, str) and host in _LOCALHOST_HOSTS:
                            return
                if event == "socket.getaddrinfo" and args:
                    host = args[0]
                    if isinstance(host, str) and host in _LOCALHOST_HOSTS:
                        return
                raise PermissionError(
                    "[agent-guard] network access is limited to localhost "
                    "in this sandbox mode"
                )
            raise PermissionError(
                "[agent-guard] network access is blocked in the agent sandbox"
            )

        if event == "import":
            name = args[0] if args else None
            if isinstance(name, str) and name.split(".", 1)[0] in _BLOCKED_IMPORTS:
                raise ImportError(
                    f"[agent-guard] import of {name.split('.', 1)[0]!r} is "
                    "blocked in the agent sandbox"
                )
            return

        if event in _WRITE_ONE_PATH:
            if args:
                _check("write", args[0], _WRITE_ROOTS)
            return

        if event in _WRITE_TWO_PATHS:
            if len(args) >= 2:
                # Two-path ops are asymmetric: args[0] is the source (a read,
                # e.g. the src of os.rename / shutil.copyfile) and args[1] is
                # the destination (the only write). Checking both against
                # write roots denied copying a file from a read-only root
                # (site-packages, the framework source) into the project.
                _check("read", args[0], _READ_ROOTS)
                _check("write", args[1], _WRITE_ROOTS)
            return

        if event == "os.truncate":
            # Audit signature varies by call form ((fd, path) vs (path,));
            # check every str/bytes argument, skip fd ints / None.
            for arg in args:
                _check("write", arg, _WRITE_ROOTS)
            return

        if event in _READ_ONE_PATH:
            if args:
                _check("read", args[0], _READ_ROOTS)
            return

    sys.addaudithook(_audit_hook)


_load_chained_sitecustomize()
