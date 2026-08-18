---
slug: "technical/db-schema"
title: "Database Schema Reference"
category: "technical"
tags: ["database", "sql-server", "schema"]
---

# Database Schema Reference

The schema below is written in SQL Server terms (the production dialect).
Since the multi-database rollout the same ORM model runs on four dialects —
see [Supported Databases](#supported-databases) for the type mapping.

## Supported Databases

All four are first-class citizens, switched via `DATABASE_URL` (see
`packages/api/src/api/config.py` for the precedence chain):

| Dialect | App driver (async) | Alembic driver (sync) | Notes |
|---|---|---|---|
| SQL Server | `mssql+aioodbc` | `mssql+pyodbc` | Default production DB; DB_* fields build its URL |
| PostgreSQL | `postgresql+asyncpg` | `postgresql+psycopg2` | CI live-job dialect |
| MySQL | `mysql+aiomysql` | `mysql+pymysql` | Create DB with `utf8mb4` + `utf8mb4_0900_bin` (case-sensitive slug uniqueness) |
| SQLite | `sqlite+aiosqlite` | `sqlite` | Test suite runs the real alembic chain per session |

### Type mapping (model → physical)

| Model (portable) | SQL Server | PostgreSQL | MySQL | SQLite |
|---|---|---|---|---|
| `String(36)` PK, client `uuid4()` | UNIQUEIDENTIFIER (no NEWID — client-side) | VARCHAR(36) | VARCHAR(36) | VARCHAR(36) |
| `Unicode(n)` | NVARCHAR(n) | VARCHAR(n) | VARCHAR(n) | VARCHAR(n) |
| `UnicodeText` | NTEXT* | TEXT | LONGTEXT | TEXT |
| `UTCDateTime` (naive UTC) | DATETIME2 | TIMESTAMP WITHOUT TIME ZONE | DATETIME | DATETIME |
| `Boolean` | BIT | BOOLEAN | TINYINT(1) | BOOLEAN |
| `Integer` | INT | INTEGER | INTEGER | INTEGER |

\* SQLAlchemy's mssql dialect maps `UnicodeText` to NTEXT by default; use a
`NVARCHAR(MAX)` variant if MAX-length semantics are needed.

Migration rules that keep the chain portable (see `packages/api/alembic/`):
- T-SQL-only DDL (`ALTER COLUMN`, `NEWID()`, `ADD CONSTRAINT ... DEFAULT`)
  sits behind `if op.get_bind().dialect.name == "mssql":` branches.
- SQLite has no `ALTER COLUMN` / named-constraint DDL — use
  `op.batch_alter_table` and named FKs/unique *indexes* where needed.
- Data backfills that touch live tables are guarded with
  `context.is_offline_mode()` so `alembic upgrade --sql` still compiles.
- MySQL specifics: unnamed constraints get engine-generated names
  (`projects_ibfk_2`, `knowledge_metadata_ibfk_1`, ...) — resolve them via
  `sa.inspect(bind).get_foreign_keys()` before dropping the index/column they
  need (errors 1553/1828; only fires when no other index serves the FK).
  Literal `DEFAULT` on TEXT/BLOB is rejected (1101) — skip the server default
  where the ORM supplies a client default. Offline reflection guards skip the
  FK resolution (`op.get_context().as_sql`) so `--sql` still compiles.
- MySQL engine runs at READ COMMITTED (`api/database.py`) — its default
  REPEATABLE READ snapshots each transaction at first read, hiding commits
  from other sessions (PG/SQL Server default to READ COMMITTED).

## Core Tables

### users
| Column | Type | Notes |
|---|---|---|
| user_id | UNIQUEIDENTIFIER PK | |
| email | NVARCHAR(255) UNIQUE | |
| display_name | NVARCHAR(255) | |
| avatar_url | NVARCHAR(500) | |
| created_at | DATETIME2 | DEFAULT SYSDATETIME() |
| last_login_at | DATETIME2 | |
| is_active | BIT | DEFAULT 1 |

### oauth_sessions
| Column | Type | Notes |
|---|---|---|
| session_id | UNIQUEIDENTIFIER PK | |
| user_id | UNIQUEIDENTIFIER FK→users | |
| provider | NVARCHAR(50) | github\|google\|azure |
| provider_uid | NVARCHAR(255) | UNIQUE(provider, provider_uid) |
| access_token | NVARCHAR(MAX) | AES-256-GCM encrypted |
| refresh_token | NVARCHAR(MAX) | encrypted |
| expires_at | DATETIME2 | |

### jwt_sessions
Stores JTI for JWT revocation.
| Column | Type | Notes |
|---|---|---|
| jti | UNIQUEIDENTIFIER PK | |
| user_id | UNIQUEIDENTIFIER FK→users | |
| issued_at | DATETIME2 | |
| expires_at | DATETIME2 | |
| revoked | BIT | DEFAULT 0 |

### user_tokens
User's API keys for LLM providers and external services.
| Column | Type | Notes |
|---|---|---|
| token_id | UNIQUEIDENTIFIER PK | |
| user_id | UNIQUEIDENTIFIER FK→users | |
| service_key | NVARCHAR(100) | e.g. "anthropic:high", "openai:mid" |
| encrypted_value | NVARCHAR(MAX) | AES-256-GCM |
| key_hint | NVARCHAR(10) | last 4 chars unencrypted |
| UNIQUE(user_id, service_key) | | |

## Project Hierarchy

### workspaces
| Column | Type | Notes |
|---|---|---|
| workspace_id | UNIQUEIDENTIFIER PK | |
| slug | NVARCHAR(100) UNIQUE | |
| display_name | NVARCHAR(255) | |
| owner_id | UNIQUEIDENTIFIER FK→users | |

### workspace_members
| Column | Type | Notes |
|---|---|---|
| workspace_id + user_id | PK | |
| role | NVARCHAR(50) | owner\|admin\|member |

### projects
| Column | Type | Notes |
|---|---|---|
| project_id | UNIQUEIDENTIFIER PK | |
| workspace_id | UNIQUEIDENTIFIER FK→workspaces | |
| parent_id | UNIQUEIDENTIFIER FK→projects | NULL = top-level |
| slug | NVARCHAR(100) | UNIQUE(workspace_id, slug) |
| display_name | NVARCHAR(255) | |
| fs_path | NVARCHAR(500) | path to projects/{ws}/{proj}/ on disk |

## Agent & Conversation

### agents
| Column | Type | Notes |
|---|---|---|
| agent_id | UNIQUEIDENTIFIER PK | |
| slug | NVARCHAR(100) UNIQUE | |
| definition_path | NVARCHAR(500) | path to .agent.md file |
| model_low | NVARCHAR(MAX) | legacy, unused at runtime — models from `user_model_configs` |
| model_mid | NVARCHAR(MAX) | legacy, unused |
| model_high | NVARCHAR(MAX) | legacy, unused |
| system_prompt | NVARCHAR(MAX) | |
| skills | NVARCHAR(MAX) | JSON array of skill slugs |
| is_system | BIT | DEFAULT 0 |

### conversations
| Column | Type | Notes |
|---|---|---|
| conversation_id | UNIQUEIDENTIFIER PK | |
| project_id | UNIQUEIDENTIFIER FK→projects | |
| agent_id | UNIQUEIDENTIFIER FK→agents | |
| user_id | UNIQUEIDENTIFIER FK→users | |
| token_budget | INT | DEFAULT 128000 |
| tokens_used | INT | DEFAULT 0 |
| status | NVARCHAR(50) | active\|archived |

### messages
| Column | Type | Notes |
|---|---|---|
| message_id | UNIQUEIDENTIFIER PK | |
| conversation_id | UNIQUEIDENTIFIER FK→conversations | |
| role | NVARCHAR(20) | user\|assistant\|tool |
| content | NVARCHAR(MAX) | |
| tool_calls | NVARCHAR(MAX) | JSON array |
| knowledge_refs | NVARCHAR(MAX) | JSON array of knowledge_ids |
| token_count | INT | |
| sequence_num | INT | |

### agent_tasks
| Column | Type | Notes |
|---|---|---|
| task_id | UNIQUEIDENTIFIER PK | |
| conversation_id | UNIQUEIDENTIFIER FK→conversations | |
| parent_task_id | UNIQUEIDENTIFIER FK→agent_tasks | NULL = top-level |
| sequence_num | INT | |
| title | NVARCHAR(500) | |
| status | NVARCHAR(50) | pending\|running\|completed\|failed\|skipped |
| tools_needed | NVARCHAR(MAX) | JSON string array |
| depends_on | NVARCHAR(MAX) | JSON task_id array |
| estimated_complexity | NVARCHAR(20) | low\|mid\|high |
| actual_model | NVARCHAR(100) | |
| result_summary | NVARCHAR(MAX) | |
| error_message | NVARCHAR(MAX) | |
| started_at | DATETIME2 | |
| completed_at | DATETIME2 | |

## Knowledge Management

### knowledge_metadata
| Column | Type | Notes |
|---|---|---|
| knowledge_id | UNIQUEIDENTIFIER PK | |
| project_id | UNIQUEIDENTIFIER FK→projects | NULL = global |
| category | NVARCHAR(50) | system\|domain\|technical\|skills\|workflows |
| slug | NVARCHAR(200) | UNIQUE(project_id, category, slug) |
| title | NVARCHAR(255) | |
| fs_path | NVARCHAR(500) | |
| embedding | NVARCHAR(MAX) | JSON float array |
| embedding_model | NVARCHAR(100) | |
| completeness_score | FLOAT | 0.0–100.0 |
| coverage_breadth | FLOAT | |
| recency_score | FLOAT | |
| cross_ref_density | FLOAT | |
| agent_gap_score | FLOAT | |
| tags | NVARCHAR(MAX) | JSON string array |
| cross_references | NVARCHAR(MAX) | JSON slug array |
| content_hash | NVARCHAR(64) | SHA-256 |
| word_count | INT | |
| last_accessed_at | DATETIME2 | |
| version | INT | DEFAULT 1 |
| is_archived | BIT | DEFAULT 0 |

### knowledge_versions
| Column | Type | Notes |
|---|---|---|
| version_id | UNIQUEIDENTIFIER PK | |
| knowledge_id | UNIQUEIDENTIFIER FK→knowledge_metadata | |
| version_num | INT | |
| content | NVARCHAR(MAX) | |
| changed_by | NVARCHAR(100) | user_id or "agent:{slug}" |
| change_summary | NVARCHAR(500) | |

## Memory Layers

### personal_memories
| Column | Type | Notes |
|---|---|---|
| memory_id | UNIQUEIDENTIFIER PK | |
| user_id | UNIQUEIDENTIFIER FK→users | |
| project_id | UNIQUEIDENTIFIER FK→projects | NULL = global personal |
| content | NVARCHAR(MAX) | |
| tags | NVARCHAR(MAX) | JSON |
| embedding | NVARCHAR(MAX) | JSON float array |
| created_by | NVARCHAR(50) | "user" or "agent:{slug}" |
| is_archived | BIT | DEFAULT 0 |

### episodic_memories
| Column | Type | Notes |
|---|---|---|
| episode_id | UNIQUEIDENTIFIER PK | |
| conversation_id | UNIQUEIDENTIFIER FK→conversations | |
| user_id | UNIQUEIDENTIFIER FK→users | |
| project_id | UNIQUEIDENTIFIER FK→projects | |
| summary | NVARCHAR(MAX) | |
| key_findings | NVARCHAR(MAX) | JSON string array |
| open_questions | NVARCHAR(MAX) | JSON string array |
| embedding | NVARCHAR(MAX) | JSON float array |
| generated_by | NVARCHAR(100) | "agent:{slug}:{model}" |

## Scripts

### automation_scripts
| Column | Type | Notes |
|---|---|---|
| script_id | UNIQUEIDENTIFIER PK | |
| project_id | UNIQUEIDENTIFIER FK→projects | |
| name | NVARCHAR(255) | |
| script_type | NVARCHAR(50) | workflow\|python\|bash\|playwright |
| content | NVARCHAR(MAX) | |
| created_by | UNIQUEIDENTIFIER FK→users | |

### script_runs
| Column | Type | Notes |
|---|---|---|
| run_id | UNIQUEIDENTIFIER PK | |
| script_id | UNIQUEIDENTIFIER FK→automation_scripts | |
| triggered_by | UNIQUEIDENTIFIER FK→users | |
| status | NVARCHAR(50) | pending\|running\|completed\|failed |
| exit_code | INT | |
| stdout_log | NVARCHAR(MAX) | |
| stderr_log | NVARCHAR(MAX) | |
| started_at | DATETIME2 | |
| completed_at | DATETIME2 | |

## Related Knowledge

- [[system/framework-overview]]
