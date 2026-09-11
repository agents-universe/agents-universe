"""Advisory validation for definition files (agents, skills, workflows).

Definitions are Markdown with YAML frontmatter. Registration is lazy and silent:
``agent_sync`` skips a project agent whose frontmatter does not parse, whose
``slug`` is missing/unsafe, or whose slug lacks the ``{project_slug}--`` prefix —
log line only, no row, so the agent simply never appears in the picker. The
skill/workflow loaders are lenient instead, falling back to the file stem.

The LLM that wrote the file cannot see any of that, so the filesystem tool runs
these checks at write/read time and hands the result back. The write still
succeeds — the caller is expected to fix the reported errors and rewrite.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import frontmatter

#: Separator between project slug and agent name in project agent slugs.
PROJECT_SLUG_SEPARATOR = "--"

#: Path-safe slug segment: lowercase letters, digits, dashes; must start alnum.
AGENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Leading ``{name}--`` of a slug — stripped before re-prefixing with the real
#: project slug, so a definition copied from another project does not end up
#: double-prefixed (``proj-a--proj-b--helper``).
_LEADING_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*--")

_AGENT_SUFFIX = ".agent.md"
_WORKFLOW_SUFFIX = ".workflow.md"


def validate_agent_slug(slug: object) -> bool:
    """Return True when the slug is safe to use as a filename segment."""
    return isinstance(slug, str) and bool(AGENT_SLUG_RE.match(slug))


def project_slug_from_workspace(project_fs_path: str | None) -> str | None:
    """Return the project slug encoded in a workspace path, if any.

    Workspaces are ``PROJECTS_ROOT/{slug}`` for root projects and
    ``PROJECTS_ROOT/{parent}/projects/{child}`` for child projects, so the
    directory name is the project slug (same derivation the API uses to build
    the required agent-slug prefix).
    """
    if not project_fs_path:
        return None
    name = Path(project_fs_path).name
    return name if validate_agent_slug(name) else None


def _norm(rel_path: str) -> str:
    return rel_path.replace("\\", "/").lstrip("/")


def _classify(rel_path: str, scope: str) -> str | None:
    """Return 'agent' | 'skill' | 'workflow', or None for non-definition paths."""
    parts = [p for p in _norm(rel_path).split("/") if p not in ("", ".")]
    if not parts:
        return None
    # Loaders skip private/mixin paths — no definition will be registered, so
    # validating them would only produce noise.
    if any(part.startswith("_") for part in parts):
        return None
    name = parts[-1]
    if name.endswith(_AGENT_SUFFIX):
        return "agent"
    if parts[0] in ("skills", "workflows") and name.endswith(".md"):
        return parts[0][:-1]
    # In a project workspace, agents/skills/ is the framework layout copied into
    # the wrong place — never loaded as a project skill, so it is worth flagging.
    # Global scope reads that same path legitimately.
    if scope == "project" and len(parts) > 1 and parts[0] == "agents" and parts[1] == "skills":
        return "skill"
    return None


def _parse(content: str) -> tuple[dict[str, Any], str | None]:
    try:
        post = frontmatter.loads(content)
        return dict(post.metadata), None
    except Exception as e:  # noqa: BLE001 — any parser error is reportable text
        # PyYAML puts the offending line on its own line; flatten it so the
        # message stays readable inside a one-line tool result.
        return {}, f"{type(e).__name__}: {' '.join(str(e).split())}"


def _list_field(value: object) -> list[str]:
    """Normalize a frontmatter list field to a list of strings.

    Mirrors ``AgentConfig._list_field`` (agent.py) — that module pulls in the
    whole tool tree, so importing it here would be circular. Behavior is
    intentionally identical: a comma-separated scalar is split, not iterated
    per character.
    """
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _known_tool_names() -> frozenset[str]:
    from .tools.registry import known_static_tool_names

    return known_static_tool_names()


def _quoted(value: str) -> str:
    """Quote a scalar for YAML — JSON strings are valid YAML double-quoted scalars."""
    return json.dumps(value, ensure_ascii=False)


def _reference_warnings(
    meta: dict[str, Any],
    *,
    project_fs_path: str | None,
    framework_root: str | None,
) -> list[str]:
    """Warn about skills/workflows references that resolve to no file."""
    if not project_fs_path and not framework_root:
        return []
    project = Path(project_fs_path) if project_fs_path else None
    framework = Path(framework_root) if framework_root else None
    warnings: list[str] = []

    for ref in _list_field(meta.get("skills")):
        candidates = []
        if project is not None:
            candidates.append(project / "skills" / f"{ref}.md")
        if framework is not None:
            candidates.append(framework / "agents" / "skills" / f"{ref}.md")
        if not any(c.is_file() for c in candidates):
            warnings.append(f"skills 引用 {ref!r} 找不到对应文件（项目 skills/{ref}.md 或框架 agents/skills/{ref}.md）")

    for ref in _list_field(meta.get("workflows")):
        candidates = []
        if project is not None:
            candidates.append(project / "workflows" / f"{ref}.workflow.md")
        if framework is not None:
            candidates.append(framework / "workflows" / f"{ref}.workflow.md")
        if not any(c.is_file() for c in candidates):
            warnings.append(f"workflows 引用 {ref!r} 找不到对应文件（workflows/{ref}.workflow.md）")

    return warnings


def _tool_warnings(meta: dict[str, Any]) -> list[str]:
    declared = _list_field(meta.get("tools"))
    if not declared:
        return []
    # MCP markers are not registry tools — they are resolved from the project's
    # MCP catalog at runtime, so they are always considered known.
    unknown = [
        t for t in declared
        if t != "mcp" and not t.startswith("mcp:") and t not in _known_tool_names()
    ]
    if not unknown:
        return []
    return [f"tools 声明了 registry 中不存在的工具：{', '.join(unknown)}（运行时会静默丢弃）"]


def _suggested_frontmatter(kind: str, slug: str | None, meta: dict[str, Any]) -> str:
    """Minimal valid frontmatter the caller can paste back in and complete."""
    slug_line = _quoted(slug) if slug else _quoted("<必填：小写字母/数字/连字符>")
    display = meta.get("display_name")
    description = meta.get("description")
    lines = ["---", f"slug: {slug_line}"]
    if kind == "agent":
        lines.append(f"display_name: {_quoted(display) if isinstance(display, str) and display else _quoted('<显示名>')}")
        lines.append(f"description: {_quoted(description) if isinstance(description, str) and description else _quoted('<一句话职责>')}")
        lines.append("tools:")
        lines.append("  - filesystem")
        lines.append("skills: []")
        lines.append("workflows: []")
    else:
        lines.append(f"description: {_quoted(description) if isinstance(description, str) and description else _quoted('<一句话说明>')}")
        if kind == "skill":
            lines.append("type: \"guidance\"")
        lines.append("triggers: []")
    lines.append("---")
    return "\n".join(lines)


def _slugify(value: str) -> str:
    """Best-effort rewrite of a value into the allowed slug charset."""
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized if normalized and normalized[0].isalnum() else ""


def _agent_expected_slug(stem: str, slug: str, project_slug: str | None) -> tuple[str | None, str | None]:
    """Return (expected_slug, required_prefix) for an agent definition.

    The suggestion is derived from the frontmatter slug when it is usable and
    from the filename stem otherwise, so a wrong slug still yields a concrete
    value to write.
    """
    prefix = f"{project_slug}{PROJECT_SLUG_SEPARATOR}" if project_slug else None
    for candidate in (slug, stem):
        if not candidate:
            continue
        if prefix and candidate.startswith(prefix):
            return candidate, prefix
        base = _slugify(_LEADING_PREFIX_RE.sub("", candidate))
        if base:
            return (f"{prefix}{base}" if prefix else base), prefix
    return None, prefix


def _check_agent_placement(rel: str, name: str) -> str | None:
    if rel == f"agents/{name}":
        return None
    if rel.startswith("agents/"):
        return f"定义文件必须直接放在 agents/ 下（当前 {rel}）：同步只扫描 agents/*.agent.md，子目录不会被注册。"
    return f"文件不在 agents/ 目录下（当前 {rel}）：不会被注册为智能体。"


def check_definition(
    rel_path: str,
    content: str,
    *,
    scope: str = "project",
    project_slug: str | None = None,
    project_fs_path: str | None = None,
    framework_root: str | None = None,
) -> dict[str, Any] | None:
    """Validate a written/read definition file; None when the path is not one.

    ``scope`` is ``"project"`` for the workspace overlay and ``"global"`` for the
    framework directory — global definitions never carry the project prefix, so
    the prefix rule only applies to project scope.
    """
    kind = _classify(rel_path, scope)
    if kind is None:
        return None

    rel = _norm(rel_path)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    name = parts[-1]
    meta, parse_error = _parse(content)
    errors: list[str] = []
    warnings: list[str] = []

    if parse_error:
        errors.append(
            f"frontmatter YAML 解析失败：{parse_error}；"
            "值里含英文冒号或 # 时必须加引号（如 description: \"...\"）。"
        )

    raw_slug = meta.get("slug")
    slug = raw_slug if isinstance(raw_slug, str) else ""
    expected: dict[str, Any] = {}

    if kind == "agent":
        stem = name[: -len(_AGENT_SUFFIX)]
        expected_slug, prefix = _agent_expected_slug(stem, slug, project_slug if scope == "project" else None)
        if prefix:
            expected["project_slug"] = project_slug
            expected["slug_prefix"] = prefix
        if expected_slug:
            expected["slug"] = expected_slug
            expected["path"] = f"agents/{expected_slug}{_AGENT_SUFFIX}"
        placement_error = _check_agent_placement(rel, name)
        if placement_error:
            errors.append(placement_error)
        # Everything below reads parsed frontmatter. After a parse failure the
        # fields are simply unknown, and reporting "missing slug" / "missing
        # display_name" alongside it would restate the symptom and point the
        # caller at the wrong fix.
        if not parse_error:
            if not slug:
                errors.append("frontmatter 缺少 slug 键：没有 slug 的智能体定义不会被注册。")
            else:
                if not validate_agent_slug(slug):
                    errors.append(
                        f"slug {slug!r} 非法：只允许小写字母、数字、连字符，且以字母或数字开头"
                        "（大写、下划线、点、中文都会被拒绝）。"
                    )
                if slug != stem:
                    errors.append(
                        f"slug {slug!r} 与文件名主干 {stem!r} 不一致：运行期按 agents/{{slug}}.agent.md 查找，"
                        "不一致会出现「选择器可见但执行失败」——请改名文件或改 slug，两者必须相同。"
                    )
                if prefix and not slug.startswith(prefix):
                    errors.append(
                        f"项目智能体 slug 必须以 {prefix!r} 开头（当前 {slug!r}）："
                        f"缺少该前缀的定义不会被注册，正确写法示例 {expected_slug!r}。"
                    )
            if not meta.get("display_name"):
                warnings.append("缺少 display_name：选择器里会退化成 slug 显示。")
            for field in ("tools", "skills", "workflows"):
                if isinstance(meta.get(field), str):
                    warnings.append(f"{field} 写成了字符串标量：建议用 YAML 列表，避免解析歧义。")
        warnings.extend(_tool_warnings(meta))

    elif kind == "skill":
        expected_slug = rel[len("skills/"): -len(".md")] if rel.startswith("skills/") else ""
        if not rel.startswith("skills/"):
            errors.append(
                f"项目技能必须写到 skills/ 下（当前 {rel}）：agents/skills/ 是框架目录布局，项目不会加载它。"
            )
        if expected_slug:
            expected["slug"] = expected_slug
        if not parse_error:
            if not slug:
                warnings.append(
                    f"缺少 slug：会以文件名主干 {Path(name).stem!r} 注册"
                    f"（约定值为 {expected_slug!r}）。"
                )
            elif expected_slug and slug != expected_slug:
                warnings.append(f"slug {slug!r} 与约定值 {expected_slug!r}（相对 skills/ 的路径）不一致。")
        warnings.extend(_tool_warnings(meta))

    else:  # workflow
        rest = rel[len("workflows/"):] if rel.startswith("workflows/") else rel
        if rest.endswith(_WORKFLOW_SUFFIX):
            expected_slug = rest[: -len(_WORKFLOW_SUFFIX)]
        elif rest.endswith(".md"):
            expected_slug = rest[: -len(".md")]
        else:
            expected_slug = ""
        if expected_slug:
            expected["slug"] = expected_slug
        if not name.endswith(_WORKFLOW_SUFFIX):
            warnings.append(
                f"文件名 {name!r} 不以 {_WORKFLOW_SUFFIX} 结尾（约定 workflows/<slug>.workflow.md）。"
            )
        if not parse_error:
            if not slug:
                hint = f"（约定值为 {expected_slug!r}）" if expected_slug else ""
                warnings.append(
                    f"缺少 slug：loader 会以 {Path(name).stem!r} 注册{hint}，"
                    "与 agent 的 workflows 引用和 /命令 都对不上。"
                )
            elif expected_slug and slug != expected_slug:
                warnings.append(f"slug {slug!r} 与约定值 {expected_slug!r} 不一致。")
        warnings.extend(_tool_warnings(meta))

    warnings.extend(
        _reference_warnings(meta, project_fs_path=project_fs_path, framework_root=framework_root)
    )

    result: dict[str, Any] = {
        "ok": not errors,
        "kind": kind,
        "scope": scope,
        "path": rel_path,
        "slug": slug or None,
        "errors": errors,
        "warnings": warnings,
    }
    if expected:
        result["expected"] = expected
    if errors:
        result["suggested_frontmatter"] = _suggested_frontmatter(kind, expected.get("slug"), meta)
    return result
