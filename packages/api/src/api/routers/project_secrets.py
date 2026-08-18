"""Project-level secrets management — secrets belong to a project, not a user."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.project import Project
from api.models.project_secret import ProjectSecret
from api.services.token_vault import encrypt_project_secret, key_hint

router = APIRouter()


class SecretCreate(BaseModel):
    # Length caps mirror the column widths (encrypted_value is String(4000)
    # and the AES-GCM ciphertext is ~4/3 of the plaintext, so value is capped
    # well below the column limit — MSSQL raises DataError (500) otherwise.
    service_key: str = Field(max_length=100)
    environment: str | None = Field(default=None, max_length=100)
    secret_name: str = Field(default="default", max_length=100)
    display_name: str | None = Field(default=None, max_length=255)
    value: str = Field(max_length=2000)


class SecretUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    value: str | None = Field(default=None, max_length=2000)


@router.get("")
async def list_secrets(
    project: Project = Depends(authorize_project),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ProjectSecret).where(
            ProjectSecret.project_id == project.project_id,
            ProjectSecret.is_active == True,  # noqa: E712
        )
    )
    secrets = result.scalars().all()
    return [
        {
            "secret_id": s.secret_id,
            "service_key": s.service_key,
            "environment": s.environment,
            "secret_name": s.secret_name,
            "display_name": s.display_name,
            "key_hint": s.key_hint,
            "created_by": s.created_by,
            "updated_at": s.updated_at.isoformat() if s.updated_at else s.created_at.isoformat() if s.created_at else None,
        }
        for s in secrets
    ]


@router.post("", status_code=201)
async def create_secret(
    body: SecretCreate,
    project: Project = Depends(authorize_project),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    # Normalize a missing environment to "" — SQL Server unique indexes treat
    # NULLs as distinct, so environment=NULL rows would never collide and two
    # concurrent creates could insert duplicates that later reads trip on.
    env = body.environment or ""
    # Match legacy NULL rows too — they predate the "" normalization; SQL
    # Server unique indexes treat NULL as distinct, so a NULL row and a ""
    # row for the same key can both exist. Prefer the "" row and collapse
    # stale NULL duplicates onto it.
    if env:
        env_clause = ProjectSecret.environment == env
    else:
        env_clause = or_(
            ProjectSecret.environment.is_(None),
            ProjectSecret.environment == "",
        )
    existing = await db.execute(
        select(ProjectSecret).where(
            ProjectSecret.project_id == project.project_id,
            ProjectSecret.service_key == body.service_key,
            env_clause,
            ProjectSecret.secret_name == body.secret_name,
        ).with_for_update()
    )
    secrets = existing.scalars().all()
    secret = None
    if secrets:
        # Survivor: the "" row when both variants exist (its environment
        # never needs an UPDATE, so no unique-index conflict with the
        # deletes); a lone NULL row is migrated to "" in place.
        secret = next((s for s in secrets if s.environment == env), secrets[0])
        for stale in secrets:
            if stale is not secret:
                await db.delete(stale)

    encrypted = encrypt_project_secret(body.value, project.project_id)
    hint = key_hint(body.value)

    if secret:
        secret.environment = env  # migrate a lone legacy NULL row to "" in place
        secret.display_name = body.display_name
        secret.encrypted_value = encrypted
        secret.key_hint = hint
        secret.updated_by = current_user.user_id
        secret.updated_at = datetime.now(timezone.utc)
        secret.is_active = True
    else:
        secret = ProjectSecret(
            project_id=project.project_id,
            service_key=body.service_key,
            environment=env,
            secret_name=body.secret_name,
            display_name=body.display_name,
            encrypted_value=encrypted,
            key_hint=hint,
            created_by=current_user.user_id,
        )
        db.add(secret)
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent creation of the same (service_key, environment,
        # secret_name) — with_for_update() locks only existing rows, so two
        # parallel POSTs can both pass the None branch above.
        await db.rollback()
        raise HTTPException(status_code=409, detail="Secret already exists (concurrent creation)")
    await db.refresh(secret)
    return {
        "secret_id": secret.secret_id,
        "service_key": secret.service_key,
        "environment": secret.environment,
        "secret_name": secret.secret_name,
        "key_hint": hint,
    }


@router.put("/{secret_id}")
async def update_secret(
    secret_id: str,
    body: SecretUpdate,
    project: Project = Depends(authorize_project),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(ProjectSecret).where(
            ProjectSecret.secret_id == secret_id,
            ProjectSecret.project_id == project.project_id,
            ProjectSecret.is_active == True,  # noqa: E712
        ).with_for_update()
    )
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    if body.value is not None:
        secret.encrypted_value = encrypt_project_secret(body.value, project.project_id)
        secret.key_hint = key_hint(body.value)
    if body.display_name is not None:
        secret.display_name = body.display_name
    secret.updated_by = current_user.user_id
    secret.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "secret_id": secret.secret_id,
        "service_key": secret.service_key,
        "key_hint": secret.key_hint,
    }


@router.delete("/{secret_id}")
async def delete_secret(
    secret_id: str,
    project: Project = Depends(authorize_project),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(ProjectSecret).where(
            ProjectSecret.secret_id == secret_id,
            ProjectSecret.project_id == project.project_id,
            ProjectSecret.is_active == True,  # noqa: E712
        ).with_for_update()
    )
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    await db.delete(secret)
    await db.commit()
    return {"deleted": secret_id}


@router.post("/{secret_id}/test")
async def test_secret(
    secret_id: str,
    project: Project = Depends(authorize_project),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Run the same live connectivity check as /api/tokens/{key}/test, but
    against a project-scoped secret (agent-configured integrations)."""
    from api.services.token_tests import test_service_token
    from api.services.token_vault import decrypt_project_secret_or_none

    result = await db.execute(
        select(ProjectSecret).where(
            ProjectSecret.secret_id == secret_id,
            ProjectSecret.project_id == project.project_id,
            ProjectSecret.is_active == True,  # noqa: E712
        )
    )
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")

    plain = decrypt_project_secret_or_none(secret.encrypted_value, project.project_id)
    if plain is None:
        raise HTTPException(status_code=400, detail="Stored secret is corrupted — delete and re-save it")
    return await test_service_token(
        secret.service_key,
        plain,
        None,
        db,
        current_user.user_id,
        project_id=str(project.project_id),
    )
