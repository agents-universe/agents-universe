"""Project category tests: template subsets per category, registry endpoint, validation."""
from __future__ import annotations

from pathlib import Path

from api.paths import KNOWLEDGE_TEMPLATE_DIR, PROJECTS_ROOT
from api.project_categories import get_categories, get_template_slugs

# 期望计数以 knowledge/categories.yaml 注册表为准,模板增删后无需手改测试
EXPECTED_COUNTS = {cat["slug"]: len(cat["templates"]) for cat in get_categories()}


def _knowledge_files(slug: str) -> list[str]:
    root = PROJECTS_ROOT / slug / "knowledge"
    if not root.exists():
        return []
    return sorted(
        str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.md")
    )


async def _create(client, name: str, category: str | None = None):
    body = {"display_name": name}
    if category is not None:
        body["category"] = category
    return await client.post("/api/projects", json=body)


async def test_create_software_default_all_templates(client):
    from api.project_categories import get_template_slugs

    resp = await _create(client, "sw-default")
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "software"
    assert body["category_label"] == "软件项目"

    files = set(_knowledge_files(body["slug"]))
    assert len(files) == EXPECTED_COUNTS["software"]
    # software 在 categories.yaml 中显式列出全部知识条目,按注册表断言;
    # 其他分类的专用知识条目(如 data-analysis 的数据条目)不应混入
    expected = {f"{s}.md" for s in (get_template_slugs("software") or set())}
    assert files == expected


async def test_create_docs_subset(client):
    resp = await _create(client, "docs-subset", "docs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "docs"

    files = _knowledge_files(body["slug"])
    assert len(files) == EXPECTED_COUNTS["docs"]
    assert "domain/context.md" in files
    assert "domain/glossary.md" in files
    assert "system/history.md" in files
    assert "environment/environment.md" in files
    assert "technical/system-architecture.md" in files
    # 软件向知识条目不应出现
    assert "technical/api-map.md" not in files
    assert "technical/technical-stack.md" not in files
    assert "integrations/custom-api.md" not in files


async def test_create_other_minimal(client):
    resp = await _create(client, "other-minimal", "other")
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "other"

    files = _knowledge_files(body["slug"])
    assert len(files) == EXPECTED_COUNTS["other"]
    assert "domain/context.md" in files
    assert "system/history.md" in files


async def test_create_data_analysis_subset(client):
    resp = await _create(client, "da-subset", "data-analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "data-analysis"

    files = _knowledge_files(body["slug"])
    assert len(files) == EXPECTED_COUNTS["data-analysis"]
    assert "domain/context.md" in files
    assert "domain/glossary.md" in files
    assert "system/history.md" in files
    assert "integrations/custom-api.md" in files
    assert "domain/metric-catalog.md" in files
    assert "domain/analysis-scenarios.md" in files
    assert "technical/data-source-map.md" in files
    assert "technical/data-model.md" in files
    assert "technical/data-pipelines.md" in files
    assert "skills/sql-patterns.md" in files
    assert "skills/analysis-patterns.md" in files
    # 软件向知识条目不应出现
    assert "technical/api-map.md" not in files
    assert "technical/page-map.md" not in files
    assert "technical/technical-stack.md" not in files
    assert "environment/environment.md" not in files


async def test_create_unknown_category_400(client):
    resp = await _create(client, "unknown-cat", "foo")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "unknown_category"
    assert detail["valid_categories"] == ["software", "data-analysis", "docs", "other"]
    # 校验先于任何写盘:没有遗留目录
    assert not (PROJECTS_ROOT / "unknown-cat").exists()


async def test_categories_endpoint(client):
    resp = await client.get("/api/projects/categories")
    assert resp.status_code == 200
    cats = resp.json()
    assert [c["slug"] for c in cats] == ["software", "data-analysis", "docs", "other"]
    assert [c["label"] for c in cats] == ["软件项目", "数据分析", "文档知识库", "其他"]
    assert [c["template_count"] for c in cats] == [
        EXPECTED_COUNTS["software"],
        EXPECTED_COUNTS["data-analysis"],
        EXPECTED_COUNTS["docs"],
        EXPECTED_COUNTS["other"],
    ]
    assert all(c["description"] for c in cats)


async def test_category_persisted_and_serialized(client):
    resp = await _create(client, "persist-docs", "docs")
    project_id = resp.json()["project_id"]

    got = await client.get(f"/api/projects/{project_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["category"] == "docs"
    assert body["category_label"] == "文档知识库"


async def test_list_projects_includes_category_and_label(client):
    await _create(client, "list-cat", "data-analysis")

    listing = await client.get("/api/projects")
    assert listing.status_code == 200
    item = next(p for p in listing.json() if p["display_name"] == "list-cat")
    assert item["category"] == "data-analysis"
    assert item["category_label"] == "数据分析"


async def test_list_projects_defaults_missing_category_to_software(client):
    # 直接写库绕过创建接口:category 列有 server_default,序列化必须回退到默认分类
    from api.database import AsyncSessionLocal
    from api.models.project import Project

    async with AsyncSessionLocal() as db:
        db.add(Project(display_name="no-cat-row", slug="no-cat-row"))
        await db.commit()

    listing = await client.get("/api/projects")
    item = next(p for p in listing.json() if p["display_name"] == "no-cat-row")
    assert item["category"] == "software"
    assert item["category_label"] == "软件项目"
