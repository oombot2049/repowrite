from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional
from uuid import uuid4

import aiosqlite
from pydantic import TypeAdapter

from nexapilot.evaluation.feedback import EvalCandidate, RunFeedback
from nexapilot.model import (
    Artifact,
    CoreMemoryBlock,
    CronJob,
    CronJobRun,
    CronJobState,
    CronPayload,
    CronSchedule,
    Episode,
    EpisodeHit,
    MemoryCheckpoint,
    Message,
    MessageWithParts,
    ModelRef,
    OutboxEvent,
    Part,
    PermissionRequest,
    PermissionRule,
    Project,
    Run,
    RunStep,
    SemanticMemory,
    SemanticMemoryHit,
    Session,
    TodoItem,
)


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self._path = path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            journal_cursor = await db.execute("PRAGMA journal_mode=WAL;")
            await journal_cursor.close()
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  root_path TEXT NOT NULL COLLATE NOCASE UNIQUE,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  last_opened_at INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  worktree TEXT NOT NULL,
                  cwd TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  permission_rules_json TEXT NOT NULL,
                  kind TEXT NOT NULL DEFAULT 'primary',
                  agent_name TEXT NOT NULL DEFAULT 'primary',
                  root_session_id TEXT,
                  parent_session_id TEXT,
                  parent_tool_call_id TEXT,
                  runtime_backend TEXT NOT NULL DEFAULT 'local',
                  runtime_json TEXT NOT NULL DEFAULT '{}',
                  project_worktree TEXT,
                  project_id TEXT
                )
                """,
            )
            await self._migrate_sessions_runtime_columns(db)
            await self._migrate_sessions_agent_columns(db)
            await self._migrate_sessions_project_worktree(db)
            await self._migrate_sessions_project_id(db)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_project_updated "
                "ON sessions(project_id, updated_at DESC)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_workspaces (
                  id TEXT PRIMARY KEY,
                  child_session_id TEXT NOT NULL UNIQUE,
                  root_session_id TEXT NOT NULL,
                  repository_root TEXT NOT NULL,
                  worktree_path TEXT NOT NULL UNIQUE,
                  branch_name TEXT NOT NULL UNIQUE,
                  base_commit TEXT NOT NULL,
                  head_commit TEXT,
                  status TEXT NOT NULL,
                  cleanup_policy TEXT NOT NULL,
                  dirty INTEGER NOT NULL DEFAULT 0,
                  error TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  released_at INTEGER
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_workspaces_root "
                "ON agent_workspaces(root_session_id, created_at)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  run_id TEXT,
                  message_id TEXT,
                  tool_call_id TEXT,
                  kind TEXT NOT NULL,
                  name TEXT NOT NULL,
                  media_type TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL,
                  sha256 TEXT NOT NULL,
                  storage_path TEXT NOT NULL UNIQUE,
                  preview TEXT NOT NULL DEFAULT '',
                  created_at INTEGER NOT NULL
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_run "
                "ON artifacts(run_id, created_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_session "
                "ON artifacts(session_id, created_at)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  trigger_message_id TEXT,
                  assistant_message_id TEXT,
                  status TEXT NOT NULL,
                  source TEXT NOT NULL,
                  agent_name TEXT NOT NULL,
                  model_provider TEXT NOT NULL,
                  model_id TEXT NOT NULL,
                  started_at INTEGER,
                  completed_at INTEGER,
                  finish_reason TEXT,
                  error_json TEXT,
                  model_rounds INTEGER NOT NULL DEFAULT 0,
                  tool_call_count INTEGER NOT NULL DEFAULT 0,
                  input_sequence INTEGER,
                  output_sequence INTEGER,
                  revision INTEGER NOT NULL DEFAULT 0,
                  state_reason TEXT,
                  owner_id TEXT,
                  lease_until INTEGER,
                  heartbeat_at INTEGER,
                  attempt INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL DEFAULT 1,
                  next_attempt_at INTEGER,
                  checkpoint_seq INTEGER NOT NULL DEFAULT 0,
                  checkpoint_json TEXT,
                  cancel_requested_at INTEGER,
                  error_code TEXT,
                  error_summary TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  UNIQUE(session_id, sequence)
                )
                """,
            )
            await self._migrate_run_lifecycle_columns(db)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_session_sequence ON runs(session_id, sequence DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, updated_at)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS run_feedback (
                  id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL UNIQUE,
                  session_id TEXT NOT NULL,
                  rating TEXT NOT NULL,
                  error_types_json TEXT NOT NULL,
                  comment_redacted TEXT NOT NULL DEFAULT '',
                  redaction_count INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_feedback_session_created "
                "ON run_feedback(session_id, created_at DESC)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_candidates (
                  id TEXT PRIMARY KEY,
                  feedback_id TEXT NOT NULL UNIQUE,
                  run_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  prompt_redacted TEXT NOT NULL,
                  response_redacted TEXT NOT NULL,
                  error_types_json TEXT NOT NULL,
                  feedback_redacted TEXT NOT NULL DEFAULT '',
                  run_status TEXT NOT NULL,
                  source_message_ids_json TEXT NOT NULL,
                  redaction_count INTEGER NOT NULL DEFAULT 0,
                  reviewer_note TEXT NOT NULL DEFAULT '',
                  reviewed_at INTEGER,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_candidates_status_created "
                "ON eval_candidates(status, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_candidates_session_created "
                "ON eval_candidates(session_id, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_lease ON runs(status, lease_until)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS session_leases (
                  session_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  owner_id TEXT NOT NULL,
                  lease_until INTEGER NOT NULL,
                  revision INTEGER NOT NULL DEFAULT 0,
                  updated_at INTEGER NOT NULL
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_leases_expiry ON session_leases(lease_until)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS run_steps (
                  id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  status TEXT NOT NULL,
                  input_ref TEXT,
                  output_ref TEXT,
                  operation_id TEXT,
                  started_at INTEGER NOT NULL,
                  finished_at INTEGER,
                  error_code TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(run_id, sequence)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id, sequence)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_calls (
                  id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  step_id TEXT NOT NULL,
                  parent_call_id TEXT,
                  provider TEXT NOT NULL,
                  endpoint_hash TEXT NOT NULL,
                  model TEXT NOT NULL,
                  transport TEXT NOT NULL,
                  capability_profile_version TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  status TEXT NOT NULL,
                  semantic_output_started INTEGER NOT NULL DEFAULT 0,
                  fallback_from_call_id TEXT,
                  retry_reason TEXT,
                  started_at INTEGER NOT NULL,
                  finished_at INTEGER,
                  first_event_at INTEGER,
                  input_tokens INTEGER,
                  output_tokens INTEGER,
                  cached_tokens INTEGER,
                  reasoning_tokens INTEGER,
                  estimated_cost_microusd INTEGER,
                  pricing_version TEXT,
                  error_code TEXT,
                  public_error TEXT,
                  provider_request_id TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id, started_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_calls_status ON llm_calls(status, started_at)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_call_attempts (
                  id TEXT PRIMARY KEY,
                  call_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  started_at INTEGER NOT NULL,
                  connected_at INTEGER,
                  first_event_at INTEGER,
                  finished_at INTEGER,
                  semantic_output_started INTEGER NOT NULL DEFAULT 0,
                  http_status INTEGER,
                  provider_code TEXT,
                  provider_request_id TEXT,
                  retry_after_ms INTEGER,
                  error_code TEXT,
                  diagnostic_summary TEXT,
                  input_tokens INTEGER,
                  output_tokens INTEGER,
                  cached_tokens INTEGER,
                  reasoning_tokens INTEGER,
                  UNIQUE(call_id, attempt)
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_attempts_call ON llm_call_attempts(call_id, attempt)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_circuits (
                  circuit_key TEXT PRIMARY KEY,
                  state TEXT NOT NULL,
                  failure_count INTEGER NOT NULL DEFAULT 0,
                  window_started_at INTEGER,
                  opened_at INTEGER,
                  retry_at INTEGER,
                  half_open_owner TEXT,
                  revision INTEGER NOT NULL DEFAULT 0,
                  updated_at INTEGER NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_events (
                  id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  event_type TEXT NOT NULL,
                  aggregate_type TEXT NOT NULL,
                  aggregate_id TEXT NOT NULL,
                  session_id TEXT,
                  run_id TEXT,
                  sequence_from INTEGER,
                  sequence_to INTEGER,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0,
                  next_retry_at INTEGER,
                  claimed_at INTEGER,
                  claimed_by TEXT,
                  last_error TEXT,
                  created_at INTEGER NOT NULL,
                  processed_at INTEGER
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_events(status, next_retry_at, created_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_run ON outbox_events(run_id, created_at)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_checkpoints (
                  processor_name TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  last_message_sequence INTEGER NOT NULL DEFAULT 0,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY(processor_name, session_id)
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_episodes (
                  id TEXT PRIMARY KEY,
                  workspace TEXT NOT NULL,
                  source_session_id TEXT NOT NULL,
                  source_run_id TEXT NOT NULL UNIQUE,
                  source_kind TEXT NOT NULL DEFAULT 'primary',
                  source_agent TEXT NOT NULL DEFAULT 'primary',
                  sequence_from INTEGER NOT NULL,
                  sequence_to INTEGER NOT NULL,
                  goal TEXT NOT NULL,
                  actions_json TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  errors_json TEXT NOT NULL,
                  artifacts_json TEXT NOT NULL,
                  lessons_json TEXT NOT NULL,
                  extractor_version TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                )
                """,
            )
            await self._migrate_memory_episode_source_columns(db)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_episodes_workspace "
                "ON memory_episodes(workspace, updated_at DESC)"
            )
            await db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_episodes_fts
                USING fts5(
                  episode_id UNINDEXED,
                  workspace UNINDEXED,
                  content,
                  tokenize='unicode61'
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_memories (
                  id TEXT PRIMARY KEY,
                  namespace TEXT NOT NULL,
                  workspace TEXT NOT NULL,
                  memory_type TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  predicate TEXT NOT NULL,
                  value TEXT NOT NULL,
                  status TEXT NOT NULL,
                  confidence REAL NOT NULL,
                  importance REAL NOT NULL,
                  valid_from INTEGER,
                  valid_to INTEGER,
                  source_session_id TEXT NOT NULL,
                  source_run_id TEXT,
                  source_kind TEXT NOT NULL DEFAULT 'primary',
                  source_agent TEXT NOT NULL DEFAULT 'primary',
                  source_message_ids_json TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  extractor_version TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  CHECK(confidence >= 0 AND confidence <= 1),
                  CHECK(importance >= 0 AND importance <= 1),
                  CHECK(version >= 1)
                )
                """,
            )
            await self._migrate_semantic_memory_source_columns(db)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_semantic_memories_lookup "
                "ON semantic_memories(workspace, namespace, status, memory_type, updated_at DESC)"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_memories_active_key "
                "ON semantic_memories(workspace, namespace, memory_type, subject, predicate) "
                "WHERE status='active'"
            )
            await db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS semantic_memories_fts
                USING fts5(
                  memory_id UNINDEXED,
                  workspace UNINDEXED,
                  namespace UNINDEXED,
                  content,
                  tokenize='unicode61'
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS core_memory_blocks (
                  id TEXT PRIMARY KEY,
                  workspace TEXT NOT NULL,
                  namespace TEXT NOT NULL,
                  block_type TEXT NOT NULL,
                  content TEXT NOT NULL,
                  source_memory_ids_json TEXT NOT NULL,
                  priority INTEGER NOT NULL,
                  token_count INTEGER NOT NULL,
                  content_hash TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  UNIQUE(workspace, namespace, block_type)
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_sessions (
                  channel TEXT NOT NULL,
                  chat_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  sender_id TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY (channel, chat_id)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_sessions_session_id ON channel_sessions(session_id)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  run_id TEXT,
                  sequence INTEGER NOT NULL,
                  role TEXT NOT NULL,
                  parent_id TEXT,
                  agent TEXT NOT NULL,
                  model_provider TEXT NOT NULL,
                  model_id TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  completed_at INTEGER,
                  finish TEXT,
                  error_json TEXT,
                  tool_call_id TEXT,
                  tool_name TEXT
                )
                """,
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_sequence ON messages(session_id, sequence)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_run_sequence ON messages(run_id, sequence)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_operations (
                  operation_id TEXT PRIMARY KEY,
                  run_id TEXT,
                  session_id TEXT NOT NULL,
                  message_id TEXT NOT NULL,
                  tool_call_id TEXT NOT NULL,
                  tool_name TEXT NOT NULL,
                  capability TEXT NOT NULL,
                  canonical_target TEXT NOT NULL,
                  executor_backend TEXT NOT NULL,
                  isolation_level TEXT NOT NULL,
                  status TEXT NOT NULL,
                  error_code TEXT,
                  input_json TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  finished_at INTEGER NOT NULL,
                  UNIQUE(session_id, tool_call_id)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_operations_run ON tool_operations(run_id, created_at)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS parts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  message_id TEXT NOT NULL,
                  type TEXT NOT NULL,
                  content_json TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS permission_requests (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  permission TEXT NOT NULL,
                  patterns_json TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  always_json TEXT NOT NULL,
                  tool_json TEXT,
                  status TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  resolved_at INTEGER
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS permission_approvals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  permission TEXT NOT NULL,
                  pattern TEXT NOT NULL,
                  action TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                  id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  content TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  priority TEXT NOT NULL DEFAULT 'medium',
                  active_form TEXT NOT NULL,
                  position INTEGER NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY (session_id, id)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_todos_session ON todos(session_id)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS goals (
                  id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  active_plan_id TEXT,
                  revision INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_session ON goals(session_id, updated_at DESC)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS task_plans (
                  id TEXT PRIMARY KEY,
                  goal_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  rationale TEXT NOT NULL DEFAULT '',
                  source_run_id TEXT,
                  revision INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  UNIQUE(goal_id, version)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_plans_goal ON task_plans(goal_id, version DESC)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS plan_tasks (
                  id TEXT PRIMARY KEY,
                  plan_id TEXT NOT NULL,
                  task_key TEXT NOT NULL,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 0,
                  position INTEGER NOT NULL,
                  execution_mode TEXT NOT NULL DEFAULT 'agent',
                  assignee TEXT,
                  attempt INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL DEFAULT 1,
                  next_attempt_at INTEGER,
                  owner_id TEXT,
                  lease_until INTEGER,
                  last_run_id TEXT,
                  result_json TEXT,
                  error_json TEXT,
                  revision INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  UNIQUE(plan_id, task_key)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_plan_tasks_sched "
                "ON plan_tasks(plan_id, status, execution_mode, position)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS plan_task_dependencies (
                  task_id TEXT NOT NULL,
                  depends_on_task_id TEXT NOT NULL,
                  PRIMARY KEY(task_id, depends_on_task_id),
                  CHECK(task_id <> depends_on_task_id)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_dependencies_parent "
                "ON plan_task_dependencies(depends_on_task_id)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS task_runtime_events (
                  id TEXT PRIMARY KEY,
                  sequence INTEGER NOT NULL,
                  session_id TEXT NOT NULL,
                  goal_id TEXT NOT NULL,
                  plan_id TEXT,
                  task_id TEXT,
                  event_type TEXT NOT NULL,
                  actor TEXT NOT NULL,
                  reason TEXT,
                  from_status TEXT,
                  to_status TEXT,
                  run_id TEXT,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at INTEGER NOT NULL,
                  UNIQUE(goal_id, sequence)
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_events_goal "
                "ON task_runtime_events(goal_id, sequence)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_jobs (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  schedule_kind TEXT NOT NULL,
                  schedule_at_ms INTEGER,
                  schedule_every_ms INTEGER,
                  schedule_expr TEXT,
                  schedule_tz TEXT,
                  payload_kind TEXT NOT NULL,
                  payload_message TEXT NOT NULL,
                  next_run_at_ms INTEGER,
                  last_run_at_ms INTEGER,
                  last_status TEXT,
                  last_error TEXT,
                  last_assistant_message_id TEXT,
                  last_trace_id TEXT,
                  delete_after_run INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cron_jobs_next_run ON cron_jobs(enabled, next_run_at_ms)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_job_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  started_at INTEGER NOT NULL,
                  finished_at INTEGER,
                  status TEXT NOT NULL,
                  error TEXT,
                  assistant_message_id TEXT,
                  trace_id TEXT
                )
                """,
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cron_job_runs_job ON cron_job_runs(job_id, started_at DESC)"
            )
            await db.commit()

    async def _migrate_sessions_runtime_columns(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("PRAGMA table_info(sessions)")
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        cols = {str(r[1]) for r in rows}
        if "runtime_backend" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN runtime_backend TEXT NOT NULL DEFAULT 'local'")
        if "runtime_json" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN runtime_json TEXT NOT NULL DEFAULT '{}'")

    async def _migrate_sessions_agent_columns(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("PRAGMA table_info(sessions)")
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        cols = {str(r[1]) for r in rows}
        if "kind" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'primary'")
        if "agent_name" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN agent_name TEXT NOT NULL DEFAULT 'primary'")
        if "root_session_id" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN root_session_id TEXT")
        if "parent_session_id" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")
        if "parent_tool_call_id" not in cols:
            await db.execute("ALTER TABLE sessions ADD COLUMN parent_tool_call_id TEXT")
        await db.execute("UPDATE sessions SET root_session_id = id WHERE root_session_id IS NULL OR root_session_id = ''")

    async def _migrate_sessions_project_worktree(
        self, db: aiosqlite.Connection
    ) -> None:
        columns = await self._table_columns(db, "sessions")
        if "project_worktree" not in columns:
            await db.execute("ALTER TABLE sessions ADD COLUMN project_worktree TEXT")

    async def _migrate_sessions_project_id(self, db: aiosqlite.Connection) -> None:
        columns = await self._table_columns(db, "sessions")
        if "project_id" not in columns:
            await db.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT")

    async def _migrate_run_lifecycle_columns(self, db: aiosqlite.Connection) -> None:
        columns = await self._table_columns(db, "runs")
        definitions = {
            "revision": "INTEGER NOT NULL DEFAULT 0",
            "state_reason": "TEXT",
            "owner_id": "TEXT",
            "lease_until": "INTEGER",
            "heartbeat_at": "INTEGER",
            "attempt": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 1",
            "next_attempt_at": "INTEGER",
            "checkpoint_seq": "INTEGER NOT NULL DEFAULT 0",
            "checkpoint_json": "TEXT",
            "cancel_requested_at": "INTEGER",
            "error_code": "TEXT",
            "error_summary": "TEXT",
        }
        for name, sql_type in definitions.items():
            if name not in columns:
                await db.execute(f"ALTER TABLE runs ADD COLUMN {name} {sql_type}")

    @staticmethod
    async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
        cur = await db.execute(f"PRAGMA table_info({table})")
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        return {str(row[1]) for row in rows}

    async def _migrate_memory_episode_source_columns(self, db: aiosqlite.Connection) -> None:
        columns = await self._table_columns(db, "memory_episodes")
        if "source_kind" not in columns:
            await db.execute(
                "ALTER TABLE memory_episodes ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'primary'"
            )
        if "source_agent" not in columns:
            await db.execute(
                "ALTER TABLE memory_episodes ADD COLUMN source_agent TEXT NOT NULL DEFAULT 'primary'"
            )

    async def _migrate_semantic_memory_source_columns(self, db: aiosqlite.Connection) -> None:
        columns = await self._table_columns(db, "semantic_memories")
        if "source_kind" not in columns:
            await db.execute(
                "ALTER TABLE semantic_memories ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'primary'"
            )
        if "source_agent" not in columns:
            await db.execute(
                "ALTER TABLE semantic_memories ADD COLUMN source_agent TEXT NOT NULL DEFAULT 'primary'"
            )

    def _session_from_row(self, row: tuple[Any, ...]) -> Session:
        rules = [PermissionRule.model_validate(x) for x in json.loads(row[6])]
        runtime_json: dict[str, Any]
        try:
            runtime_json = json.loads(row[13]) if row[13] else {}
        except Exception:
            runtime_json = {}
        if not isinstance(runtime_json, dict):
            runtime_json = {}
        if "backend" not in runtime_json:
            runtime_json["backend"] = row[12] or "local"
        root_session_id = row[9] or row[0]
        return Session(
            id=row[0],
            title=row[1],
            worktree=row[2],
            cwd=row[3],
            created_at=row[4],
            updated_at=row[5],
            permission_rules=rules,
            kind=row[7] or "primary",
            agent_name=row[8] or "primary",
            root_session_id=root_session_id,
            parent_session_id=row[10],
            parent_tool_call_id=row[11],
            runtime=runtime_json,
            project_worktree=row[14] if len(row) > 14 else None,
            project_id=row[15] if len(row) > 15 else None,
        )

    @staticmethod
    def _project_from_row(row: tuple[Any, ...]) -> Project:
        return Project(
            id=row[0],
            name=row[1],
            root_path=row[2],
            created_at=row[3],
            updated_at=row[4],
            last_opened_at=row[5],
        )

    async def create_project(self, project: Project) -> Project:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO projects(id,name,root_path,created_at,updated_at,last_opened_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    project.id,
                    project.name,
                    project.root_path,
                    project.created_at,
                    project.updated_at,
                    project.last_opened_at,
                ),
            )
            await db.commit()
        return await self.get_project(project.id)

    async def get_project(self, project_id: str) -> Project:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id,name,root_path,created_at,updated_at,last_opened_at
                FROM projects WHERE id=?
                """,
                (project_id,),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if not row:
            raise KeyError(f"project not found: {project_id}")
        return self._project_from_row(row)

    async def list_projects(self, *, limit: int = 100, offset: int = 0) -> list[Project]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id,name,root_path,created_at,updated_at,last_opened_at
                FROM projects
                ORDER BY last_opened_at DESC, updated_at DESC, name COLLATE NOCASE ASC
                LIMIT ? OFFSET ?
                """,
                (max(1, limit), max(0, offset)),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._project_from_row(row) for row in rows]

    async def update_project_name(self, project_id: str, name: str) -> Project:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "UPDATE projects SET name=?, updated_at=? WHERE id=?",
                (name, now, project_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"project not found: {project_id}")
            await db.commit()
        return await self.get_project(project_id)

    async def touch_project(self, project_id: str) -> Project:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "UPDATE projects SET last_opened_at=?, updated_at=? WHERE id=?",
                (now, now, project_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"project not found: {project_id}")
            await db.commit()
        return await self.get_project(project_id)

    async def delete_project(self, project_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id=?", (project_id,)
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
            if row and int(row[0]) > 0:
                raise ValueError("project still contains threads")
            cursor = await db.execute("DELETE FROM projects WHERE id=?", (project_id,))
            if cursor.rowcount == 0:
                raise KeyError(f"project not found: {project_id}")
            await db.commit()

    async def create_session(self, session: Session) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO sessions (
                  id,title,worktree,cwd,created_at,updated_at,permission_rules_json,
                  kind,agent_name,root_session_id,parent_session_id,parent_tool_call_id,
                  runtime_backend,runtime_json,project_worktree,project_id
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session.id,
                    session.title,
                    session.worktree,
                    session.cwd,
                    session.created_at,
                    session.updated_at,
                    json.dumps([r.model_dump() for r in session.permission_rules]),
                    session.kind,
                    session.agent_name,
                    session.root_session_id or session.id,
                    session.parent_session_id,
                    session.parent_tool_call_id,
                    session.runtime.backend,
                    json.dumps(session.runtime.model_dump()),
                    session.project_worktree,
                    session.project_id,
                ),
            )
            await db.commit()

    @staticmethod
    def _agent_workspace_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0],
            "child_session_id": row[1],
            "root_session_id": row[2],
            "repository_root": row[3],
            "worktree_path": row[4],
            "branch_name": row[5],
            "base_commit": row[6],
            "head_commit": row[7],
            "status": row[8],
            "cleanup_policy": row[9],
            "dirty": bool(row[10]),
            "error": row[11],
            "created_at": row[12],
            "updated_at": row[13],
            "released_at": row[14],
        }

    async def create_agent_workspace(self, record: dict[str, Any]) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO agent_workspaces (
                  id,child_session_id,root_session_id,repository_root,
                  worktree_path,branch_name,base_commit,head_commit,status,
                  cleanup_policy,dirty,error,created_at,updated_at,released_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"],
                    record["child_session_id"],
                    record["root_session_id"],
                    record["repository_root"],
                    record["worktree_path"],
                    record["branch_name"],
                    record["base_commit"],
                    record.get("head_commit"),
                    record["status"],
                    record["cleanup_policy"],
                    int(bool(record.get("dirty", False))),
                    record.get("error"),
                    record["created_at"],
                    record["updated_at"],
                    record.get("released_at"),
                ),
            )
            await db.commit()

    async def update_agent_workspace(
        self, workspace_id: str, **changes: Any
    ) -> dict[str, Any]:
        allowed = {
            "head_commit",
            "status",
            "dirty",
            "error",
            "updated_at",
            "released_at",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported agent workspace fields: {sorted(unknown)}")
        if not changes:
            return await self.get_agent_workspace(workspace_id)
        columns = []
        values = []
        for name, value in changes.items():
            columns.append(f"{name}=?")
            values.append(int(value) if name == "dirty" else value)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                f"UPDATE agent_workspaces SET {', '.join(columns)} WHERE id=?",
                (*values, workspace_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"agent workspace not found: {workspace_id}")
            await db.commit()
        return await self.get_agent_workspace(workspace_id)

    async def get_agent_workspace(self, workspace_id: str) -> dict[str, Any]:
        return await self._get_agent_workspace("id", workspace_id)

    async def get_agent_workspace_for_session(
        self, child_session_id: str
    ) -> dict[str, Any]:
        return await self._get_agent_workspace("child_session_id", child_session_id)

    async def _get_agent_workspace(
        self, column: str, value: str
    ) -> dict[str, Any]:
        if column not in {"id", "child_session_id"}:
            raise ValueError(f"unsupported agent workspace lookup: {column}")
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                f"""
                SELECT id,child_session_id,root_session_id,repository_root,
                       worktree_path,branch_name,base_commit,head_commit,status,
                       cleanup_policy,dirty,error,created_at,updated_at,released_at
                FROM agent_workspaces WHERE {column}=?
                """,
                (value,),
            )
            row = await cur.fetchone()
        if not row:
            raise KeyError(f"agent workspace not found: {value}")
        return self._agent_workspace_from_row(row)

    async def list_agent_workspaces(
        self, *, root_session_id: str | None = None
    ) -> list[dict[str, Any]]:
        where = "WHERE root_session_id=?" if root_session_id else ""
        params = (root_session_id,) if root_session_id else ()
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                f"""
                SELECT id,child_session_id,root_session_id,repository_root,
                       worktree_path,branch_name,base_commit,head_commit,status,
                       cleanup_policy,dirty,error,created_at,updated_at,released_at
                FROM agent_workspaces {where}
                ORDER BY created_at DESC
                """,
                params,
            )
            rows = await cur.fetchall()
        return [self._agent_workspace_from_row(row) for row in rows]

    @staticmethod
    def _run_from_row(row: tuple[Any, ...]) -> Run:
        return Run(
            id=row[0],
            session_id=row[1],
            sequence=row[2],
            trigger_message_id=row[3],
            assistant_message_id=row[4],
            status=row[5],
            source=row[6],
            agent_name=row[7],
            model=ModelRef(provider=row[8], id=row[9]),
            started_at=row[10],
            completed_at=row[11],
            finish_reason=row[12],
            error=json.loads(row[13]) if row[13] else None,
            model_rounds=row[14],
            tool_call_count=row[15],
            input_sequence=row[16],
            output_sequence=row[17],
            revision=row[18],
            state_reason=row[19],
            owner_id=row[20],
            lease_until=row[21],
            heartbeat_at=row[22],
            attempt=row[23],
            max_attempts=row[24],
            next_attempt_at=row[25],
            checkpoint_seq=row[26],
            checkpoint=json.loads(row[27]) if row[27] else None,
            cancel_requested_at=row[28],
            error_code=row[29],
            error_summary=row[30],
            created_at=row[31],
            updated_at=row[32],
        )

    @staticmethod
    def _outbox_event_from_row(row: tuple[Any, ...]) -> OutboxEvent:
        return OutboxEvent(
            id=row[0],
            idempotency_key=row[1],
            event_type=row[2],
            aggregate_type=row[3],
            aggregate_id=row[4],
            session_id=row[5],
            run_id=row[6],
            sequence_from=row[7],
            sequence_to=row[8],
            payload=json.loads(row[9]),
            status=row[10],
            attempts=row[11],
            next_retry_at=row[12],
            claimed_at=row[13],
            claimed_by=row[14],
            last_error=row[15],
            created_at=row[16],
            processed_at=row[17],
        )

    @staticmethod
    def _episode_from_row(row: tuple[Any, ...] | list[Any]) -> Episode:
        return Episode(
            id=row[0],
            workspace=row[1],
            source_session_id=row[2],
            source_run_id=row[3],
            source_kind=row[4],
            source_agent=row[5],
            sequence_from=row[6],
            sequence_to=row[7],
            goal=row[8],
            actions=json.loads(row[9]),
            outcome=row[10],
            errors=json.loads(row[11]),
            artifacts=json.loads(row[12]),
            lessons=json.loads(row[13]),
            extractor_version=row[14],
            created_at=row[15],
            updated_at=row[16],
        )

    @classmethod
    def _episode_search_text(cls, episode: Episode) -> str:
        return cls._fts_document("\n".join(
            [
                episode.goal,
                *episode.actions,
                episode.outcome,
                *episode.errors,
                *episode.artifacts,
                *episode.lessons,
            ]
        ))

    @staticmethod
    def _build_fts_query(
        raw: str,
        *,
        operator: str = "AND",
        expand_cjk: bool = False,
    ) -> str | None:
        tokens = [
            token.strip()
            for token in raw.replace("/", " ").replace("-", " ").split()
            if token.strip()
        ]
        if expand_cjk:
            expanded: list[str] = []
            for token in tokens:
                cjk_runs = re.findall(r"[\u3400-\u9fff]+", token)
                if not cjk_runs:
                    expanded.append(token)
                    continue
                expanded.extend(re.sub(r"[\u3400-\u9fff]+", " ", token).split())
                for run in cjk_runs:
                    expanded.extend(run[index : index + 2] for index in range(max(1, len(run) - 1)))
            tokens = expanded
        if not tokens:
            return None
        joiner = " OR " if operator.upper() == "OR" else " AND "
        return joiner.join(f'"{token.replace(chr(34), "")}"' for token in dict.fromkeys(tokens))

    @staticmethod
    def _fts_document(raw: str) -> str:
        cjk_terms: list[str] = []
        for run in re.findall(r"[\u3400-\u9fff]+", raw):
            cjk_terms.extend(run[index : index + 2] for index in range(max(1, len(run) - 1)))
        return f"{raw}\n{' '.join(cjk_terms)}" if cjk_terms else raw

    @staticmethod
    def _semantic_memory_columns() -> str:
        return (
            "id, namespace, workspace, memory_type, subject, predicate, value, status, "
            "confidence, importance, valid_from, valid_to, source_session_id, source_run_id, "
            "source_kind, source_agent, source_message_ids_json, content_hash, version, "
            "extractor_version, created_at, updated_at"
        )

    @staticmethod
    def _semantic_memory_from_row(row: tuple[Any, ...] | list[Any]) -> SemanticMemory:
        return SemanticMemory(
            id=row[0],
            namespace=row[1],
            workspace=row[2],
            memory_type=row[3],
            subject=row[4],
            predicate=row[5],
            value=row[6],
            status=row[7],
            confidence=row[8],
            importance=row[9],
            valid_from=row[10],
            valid_to=row[11],
            source_session_id=row[12],
            source_run_id=row[13],
            source_kind=row[14],
            source_agent=row[15],
            source_message_ids=json.loads(row[16]),
            content_hash=row[17],
            version=row[18],
            extractor_version=row[19],
            created_at=row[20],
            updated_at=row[21],
        )

    @classmethod
    def _semantic_memory_search_text(cls, memory: SemanticMemory) -> str:
        return cls._fts_document(
            f"{memory.memory_type}\n{memory.subject}\n{memory.predicate}\n{memory.value}"
        )

    @staticmethod
    async def _insert_semantic_memory(db: aiosqlite.Connection, memory: SemanticMemory) -> None:
        await db.execute(
            """
            INSERT INTO semantic_memories (
              id, namespace, workspace, memory_type, subject, predicate, value,
              status, confidence, importance, valid_from, valid_to,
              source_session_id, source_run_id, source_kind, source_agent,
              source_message_ids_json, content_hash, version, extractor_version,
              created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                memory.id,
                memory.namespace,
                memory.workspace,
                memory.memory_type,
                memory.subject,
                memory.predicate,
                memory.value,
                memory.status,
                memory.confidence,
                memory.importance,
                memory.valid_from,
                memory.valid_to,
                memory.source_session_id,
                memory.source_run_id,
                memory.source_kind,
                memory.source_agent,
                json.dumps(memory.source_message_ids),
                memory.content_hash,
                memory.version,
                memory.extractor_version,
                memory.created_at,
                memory.updated_at,
            ),
        )

    @staticmethod
    def _core_memory_block_from_row(row: tuple[Any, ...] | list[Any]) -> CoreMemoryBlock:
        return CoreMemoryBlock(
            id=row[0],
            workspace=row[1],
            namespace=row[2],
            block_type=row[3],
            content=row[4],
            source_memory_ids=json.loads(row[5]),
            priority=row[6],
            token_count=row[7],
            content_hash=row[8],
            version=row[9],
            created_at=row[10],
            updated_at=row[11],
        )

    async def enqueue_outbox_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        run_id: str | None = None,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        now_ms: int | None = None,
    ) -> OutboxEvent:
        if not idempotency_key.strip():
            raise ValueError("outbox idempotency key is required")
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        event_id = str(uuid4())
        payload_json = json.dumps(payload, sort_keys=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO outbox_events (
                  id, idempotency_key, event_type, aggregate_type, aggregate_id,
                  session_id, run_id, sequence_from, sequence_to, payload_json,
                  status, attempts, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    event_id,
                    idempotency_key,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    session_id,
                    run_id,
                    sequence_from,
                    sequence_to,
                    payload_json,
                    "pending",
                    0,
                    now,
                ),
            )
            await db.commit()
        event = await self.get_outbox_event_by_idempotency_key(idempotency_key)
        if (
            event.event_type != event_type
            or event.aggregate_type != aggregate_type
            or event.aggregate_id != aggregate_id
            or event.session_id != session_id
            or event.run_id != run_id
            or event.sequence_from != sequence_from
            or event.sequence_to != sequence_to
            or event.payload != payload
        ):
            raise ValueError(f"outbox idempotency key collision: {idempotency_key}")
        return event

    async def get_outbox_event(self, event_id: str) -> OutboxEvent:
        return await self._get_outbox_event("id=?", (event_id,), f"outbox event not found: {event_id}")

    async def get_outbox_event_by_idempotency_key(self, idempotency_key: str) -> OutboxEvent:
        return await self._get_outbox_event(
            "idempotency_key=?",
            (idempotency_key,),
            f"outbox event not found: {idempotency_key}",
        )

    async def _get_outbox_event(
        self,
        where: str,
        params: tuple[Any, ...],
        not_found_message: str,
    ) -> OutboxEvent:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"""
                SELECT
                  id, idempotency_key, event_type, aggregate_type, aggregate_id,
                  session_id, run_id, sequence_from, sequence_to, payload_json,
                  status, attempts, next_retry_at, claimed_at, claimed_by,
                  last_error, created_at, processed_at
                FROM outbox_events WHERE {where}
                """,
                params,
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if not row:
            raise KeyError(not_found_message)
        return self._outbox_event_from_row(row)

    async def list_outbox_events(self, *, status: str | None = None, limit: int = 100) -> list[OutboxEvent]:
        where = "WHERE status=?" if status is not None else ""
        params: tuple[Any, ...] = (status, max(1, limit)) if status is not None else (max(1, limit),)
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"""
                SELECT
                  id, idempotency_key, event_type, aggregate_type, aggregate_id,
                  session_id, run_id, sequence_from, sequence_to, payload_json,
                  status, attempts, next_retry_at, claimed_at, claimed_by,
                  last_error, created_at, processed_at
                FROM outbox_events {where}
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                params,
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._outbox_event_from_row(row) for row in rows]

    async def claim_outbox_events(
        self,
        *,
        worker_id: str,
        limit: int,
        now_ms: int,
        lease_timeout_ms: int,
        event_types: set[str] | None = None,
    ) -> list[OutboxEvent]:
        stale_before = now_ms - max(lease_timeout_ms, 1)
        claimed_ids: list[str] = []
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    UPDATE outbox_events
                    SET status='pending', claimed_at=NULL, claimed_by=NULL
                    WHERE status='processing' AND claimed_at IS NOT NULL AND claimed_at<=?
                    """,
                    (stale_before,),
                )
                type_filter = ""
                params: list[Any] = [now_ms]
                if event_types:
                    ordered_types = sorted(event_types)
                    placeholders = ",".join("?" for _ in ordered_types)
                    type_filter = f" AND event_type IN ({placeholders})"
                    params.extend(ordered_types)
                params.append(max(1, limit))
                cursor = await db.execute(
                    "SELECT id FROM outbox_events "
                    "WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at<=?)"
                    f"{type_filter} ORDER BY created_at ASC, id ASC LIMIT ?",
                    params,
                )
                try:
                    claimed_ids = [str(row[0]) for row in await cursor.fetchall()]
                finally:
                    await cursor.close()
                for event_id in claimed_ids:
                    await db.execute(
                        """
                        UPDATE outbox_events
                        SET status='processing', attempts=attempts+1,
                            claimed_at=?, claimed_by=?, last_error=NULL
                        WHERE id=? AND status='pending'
                        """,
                        (now_ms, worker_id, event_id),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return [await self.get_outbox_event(event_id) for event_id in claimed_ids]

    async def mark_outbox_processed(self, event_id: str, *, worker_id: str, now_ms: int) -> OutboxEvent:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                UPDATE outbox_events
                SET status='processed', processed_at=?, claimed_at=NULL,
                    claimed_by=NULL, next_retry_at=NULL, last_error=NULL
                WHERE id=? AND status='processing' AND claimed_by=?
                """,
                (now_ms, event_id, worker_id),
            )
            if cursor.rowcount == 0:
                await cursor.close()
                raise ValueError("outbox event is not owned by this worker")
            await cursor.close()
            await db.commit()
        return await self.get_outbox_event(event_id)

    async def mark_outbox_failed(
        self,
        event_id: str,
        *,
        worker_id: str,
        error: str,
        max_attempts: int,
        next_retry_at: int,
    ) -> OutboxEvent:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT attempts FROM outbox_events WHERE id=? AND status='processing' AND claimed_by=?",
                (event_id, worker_id),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
            if not row:
                raise ValueError("outbox event is not owned by this worker")
            dead_letter = int(row[0]) >= max(1, max_attempts)
            await db.execute(
                """
                UPDATE outbox_events
                SET status=?, next_retry_at=?, claimed_at=NULL, claimed_by=NULL, last_error=?
                WHERE id=?
                """,
                (
                    "dead_letter" if dead_letter else "pending",
                    None if dead_letter else next_retry_at,
                    error[:4000],
                    event_id,
                ),
            )
            await db.commit()
        return await self.get_outbox_event(event_id)

    async def get_memory_checkpoint(self, processor_name: str, session_id: str) -> MemoryCheckpoint:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT processor_name, session_id, last_message_sequence, updated_at
                FROM memory_checkpoints WHERE processor_name=? AND session_id=?
                """,
                (processor_name, session_id),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if row:
            return MemoryCheckpoint(
                processor_name=row[0], session_id=row[1], last_message_sequence=row[2], updated_at=row[3]
            )
        return MemoryCheckpoint(
            processor_name=processor_name, session_id=session_id, last_message_sequence=0, updated_at=0
        )

    async def advance_memory_checkpoint(
        self,
        *,
        processor_name: str,
        session_id: str,
        last_message_sequence: int,
        now_ms: int | None = None,
    ) -> MemoryCheckpoint:
        if last_message_sequence < 0:
            raise ValueError("checkpoint sequence cannot be negative")
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO memory_checkpoints (
                  processor_name, session_id, last_message_sequence, updated_at
                ) VALUES (?,?,?,?)
                ON CONFLICT(processor_name, session_id) DO UPDATE SET
                  last_message_sequence=excluded.last_message_sequence,
                  updated_at=excluded.updated_at
                WHERE excluded.last_message_sequence > memory_checkpoints.last_message_sequence
                """,
                (processor_name, session_id, last_message_sequence, now),
            )
            await db.commit()
        return await self.get_memory_checkpoint(processor_name, session_id)

    async def upsert_episode(self, episode: Episode) -> Episode:
        search_text = self._episode_search_text(episode)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO memory_episodes (
                      id, workspace, source_session_id, source_run_id, source_kind, source_agent,
                      sequence_from, sequence_to, goal, actions_json, outcome,
                      errors_json, artifacts_json, lessons_json, extractor_version,
                      created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      workspace=excluded.workspace,
                      source_session_id=excluded.source_session_id,
                      source_run_id=excluded.source_run_id,
                      source_kind=excluded.source_kind,
                      source_agent=excluded.source_agent,
                      sequence_from=excluded.sequence_from,
                      sequence_to=excluded.sequence_to,
                      goal=excluded.goal,
                      actions_json=excluded.actions_json,
                      outcome=excluded.outcome,
                      errors_json=excluded.errors_json,
                      artifacts_json=excluded.artifacts_json,
                      lessons_json=excluded.lessons_json,
                      extractor_version=excluded.extractor_version,
                      updated_at=excluded.updated_at
                    """,
                    (
                        episode.id,
                        episode.workspace,
                        episode.source_session_id,
                        episode.source_run_id,
                        episode.source_kind,
                        episode.source_agent,
                        episode.sequence_from,
                        episode.sequence_to,
                        episode.goal,
                        json.dumps(episode.actions, ensure_ascii=False),
                        episode.outcome,
                        json.dumps(episode.errors, ensure_ascii=False),
                        json.dumps(episode.artifacts, ensure_ascii=False),
                        json.dumps(episode.lessons, ensure_ascii=False),
                        episode.extractor_version,
                        episode.created_at,
                        episode.updated_at,
                    ),
                )
                await db.execute("DELETE FROM memory_episodes_fts WHERE episode_id=?", (episode.id,))
                await db.execute(
                    "INSERT INTO memory_episodes_fts(episode_id, workspace, content) VALUES (?,?,?)",
                    (episode.id, episode.workspace, search_text),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_episode(episode.id)

    async def get_episode(self, episode_id: str) -> Episode:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id, workspace, source_session_id, source_run_id,
                       source_kind, source_agent,
                       sequence_from, sequence_to, goal, actions_json, outcome,
                       errors_json, artifacts_json, lessons_json, extractor_version,
                       created_at, updated_at
                FROM memory_episodes WHERE id=?
                """,
                (episode_id,),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if not row:
            raise KeyError(f"episode not found: {episode_id}")
        return self._episode_from_row(row)

    async def list_episodes(self, workspace: str, *, limit: int = 50, offset: int = 0) -> list[Episode]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id, workspace, source_session_id, source_run_id,
                       source_kind, source_agent,
                       sequence_from, sequence_to, goal, actions_json, outcome,
                       errors_json, artifacts_json, lessons_json, extractor_version,
                       created_at, updated_at
                FROM memory_episodes
                WHERE workspace=?
                ORDER BY updated_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                (workspace, max(1, limit), max(0, offset)),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._episode_from_row(row) for row in rows]

    async def search_episodes(
        self,
        workspace: str,
        query: str,
        *,
        limit: int = 5,
        subagent_weight: float = 0.6,
    ) -> list[EpisodeHit]:
        fts_query = self._build_fts_query(query, operator="OR", expand_cjk=True)
        if not fts_query:
            return []
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT e.id, e.workspace, e.source_session_id, e.source_run_id,
                       e.source_kind, e.source_agent,
                       e.sequence_from, e.sequence_to, e.goal, e.actions_json, e.outcome,
                       e.errors_json, e.artifacts_json, e.lessons_json, e.extractor_version,
                       e.created_at, e.updated_at, bm25(memory_episodes_fts) AS rank
                FROM memory_episodes_fts
                JOIN memory_episodes e ON e.id=memory_episodes_fts.episode_id
                WHERE memory_episodes_fts MATCH ? AND e.workspace=?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (fts_query, workspace, max(1, limit) * 4),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        hits: list[EpisodeHit] = []
        for row in rows:
            rank = float(row[17]) if row[17] is not None else 999.0
            episode = self._episode_from_row(row[:17])
            trust_weight = subagent_weight if episode.source_kind == "subagent" else 1.0
            score = (1.0 / (1.0 + abs(rank))) * min(1.0, max(0.0, trust_weight))
            hits.append(EpisodeHit(episode=episode, score=score))
        hits.sort(key=lambda hit: (-hit.score, -hit.episode.updated_at, hit.episode.id))
        return hits[: max(1, limit)]

    async def delete_episode(self, episode_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("DELETE FROM memory_episodes_fts WHERE episode_id=?", (episode_id,))
                await db.execute("DELETE FROM memory_episodes WHERE id=?", (episode_id,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def put_semantic_memory(self, memory: SemanticMemory) -> SemanticMemory:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO semantic_memories (
                      id, namespace, workspace, memory_type, subject, predicate, value,
                      status, confidence, importance, valid_from, valid_to,
                      source_session_id, source_run_id, source_kind, source_agent,
                      source_message_ids_json, content_hash, version, extractor_version,
                      created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      namespace=excluded.namespace, workspace=excluded.workspace,
                      memory_type=excluded.memory_type, subject=excluded.subject,
                      predicate=excluded.predicate, value=excluded.value,
                      status=excluded.status, confidence=excluded.confidence,
                      importance=excluded.importance, valid_from=excluded.valid_from,
                      valid_to=excluded.valid_to, source_session_id=excluded.source_session_id,
                      source_run_id=excluded.source_run_id, source_kind=excluded.source_kind,
                      source_agent=excluded.source_agent,
                      source_message_ids_json=excluded.source_message_ids_json,
                      content_hash=excluded.content_hash, version=excluded.version,
                      extractor_version=excluded.extractor_version, updated_at=excluded.updated_at
                    """,
                    (
                        memory.id,
                        memory.namespace,
                        memory.workspace,
                        memory.memory_type,
                        memory.subject,
                        memory.predicate,
                        memory.value,
                        memory.status,
                        memory.confidence,
                        memory.importance,
                        memory.valid_from,
                        memory.valid_to,
                        memory.source_session_id,
                        memory.source_run_id,
                        memory.source_kind,
                        memory.source_agent,
                        json.dumps(memory.source_message_ids),
                        memory.content_hash,
                        memory.version,
                        memory.extractor_version,
                        memory.created_at,
                        memory.updated_at,
                    ),
                )
                await db.execute("DELETE FROM semantic_memories_fts WHERE memory_id=?", (memory.id,))
                if memory.status not in {"deleted", "rejected"}:
                    await db.execute(
                        """
                        INSERT INTO semantic_memories_fts(memory_id, workspace, namespace, content)
                        VALUES (?,?,?,?)
                        """,
                        (
                            memory.id,
                            memory.workspace,
                            memory.namespace,
                            self._semantic_memory_search_text(memory),
                        ),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_semantic_memory(memory.id)

    async def put_semantic_candidate_once(self, memory: SemanticMemory) -> SemanticMemory:
        """Insert a candidate once so projector replay cannot undo later governance."""
        if memory.status != "candidate":
            raise ValueError("put_semantic_candidate_once requires candidate status")
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"SELECT {self._semantic_memory_columns()} FROM semantic_memories WHERE id=?",
                    (memory.id,),
                )
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
                if row:
                    await db.rollback()
                    return self._semantic_memory_from_row(row)
                await self._insert_semantic_memory(db, memory)
                await db.execute(
                    """
                    INSERT INTO semantic_memories_fts(memory_id, workspace, namespace, content)
                    VALUES (?,?,?,?)
                    """,
                    (
                        memory.id,
                        memory.workspace,
                        memory.namespace,
                        self._semantic_memory_search_text(memory),
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_semantic_memory(memory.id)

    async def activate_semantic_memory(self, memory: SemanticMemory) -> tuple[str, SemanticMemory]:
        """Atomically ADD/SUPERSEDE/NOOP one semantic key."""
        candidate = memory.model_copy(update={"status": "active"})
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"""
                    SELECT {self._semantic_memory_columns()} FROM semantic_memories
                    WHERE workspace=? AND namespace=? AND memory_type=?
                      AND subject=? AND predicate=? AND status='active'
                    """,
                    (
                        candidate.workspace,
                        candidate.namespace,
                        candidate.memory_type,
                        candidate.subject,
                        candidate.predicate,
                    ),
                )
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
                current = self._semantic_memory_from_row(row) if row else None
                if current is not None and current.content_hash == candidate.content_hash:
                    await db.rollback()
                    return "NOOP", current

                action = "ADD"
                if current is not None:
                    action = "SUPERSEDE"
                    candidate = candidate.model_copy(update={"version": current.version + 1})
                    await db.execute(
                        """
                        UPDATE semantic_memories
                        SET status='superseded', valid_to=?, updated_at=?
                        WHERE id=? AND status='active'
                        """,
                        (candidate.valid_from or candidate.updated_at, candidate.updated_at, current.id),
                    )

                await self._insert_semantic_memory(db, candidate)
                await db.execute(
                    """
                    INSERT INTO semantic_memories_fts(memory_id, workspace, namespace, content)
                    VALUES (?,?,?,?)
                    """,
                    (
                        candidate.id,
                        candidate.workspace,
                        candidate.namespace,
                        self._semantic_memory_search_text(candidate),
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return action, await self.get_semantic_memory(candidate.id)

    async def promote_semantic_memory(
        self,
        memory_id: str,
        *,
        now_ms: int | None = None,
    ) -> tuple[str, SemanticMemory]:
        """Promote one reviewed candidate without bypassing semantic versioning."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"SELECT {self._semantic_memory_columns()} FROM semantic_memories WHERE id=?",
                    (memory_id,),
                )
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
                if not row:
                    raise KeyError(f"semantic memory not found: {memory_id}")
                candidate = self._semantic_memory_from_row(row)
                if candidate.status == "active":
                    await db.rollback()
                    return "NOOP", candidate
                if candidate.status != "candidate":
                    raise ValueError(
                        f"semantic memory cannot be promoted from status: {candidate.status}"
                    )

                cursor = await db.execute(
                    f"""
                    SELECT {self._semantic_memory_columns()} FROM semantic_memories
                    WHERE workspace=? AND namespace=? AND memory_type=?
                      AND subject=? AND predicate=? AND status='active'
                    """,
                    (
                        candidate.workspace,
                        candidate.namespace,
                        candidate.memory_type,
                        candidate.subject,
                        candidate.predicate,
                    ),
                )
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
                current = self._semantic_memory_from_row(row) if row else None
                if current is not None and current.content_hash == candidate.content_hash:
                    await db.execute(
                        "UPDATE semantic_memories SET status='rejected', valid_to=?, updated_at=? WHERE id=?",
                        (now, now, candidate.id),
                    )
                    await db.execute(
                        "DELETE FROM semantic_memories_fts WHERE memory_id=?",
                        (candidate.id,),
                    )
                    await db.commit()
                    return "NOOP", current

                action = "ADD"
                version = 1
                if current is not None:
                    action = "SUPERSEDE"
                    version = current.version + 1
                    await db.execute(
                        """
                        UPDATE semantic_memories
                        SET status='superseded', valid_to=?, updated_at=?
                        WHERE id=? AND status='active'
                        """,
                        (now, now, current.id),
                    )
                await db.execute(
                    """
                    UPDATE semantic_memories
                    SET status='active', valid_from=?, valid_to=NULL, version=?, updated_at=?
                    WHERE id=? AND status='candidate'
                    """,
                    (now, version, now, candidate.id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return action, await self.get_semantic_memory(memory_id)

    async def delete_active_semantic_memory(
        self,
        *,
        workspace: str,
        namespace: str,
        memory_type: str,
        subject: str,
        predicate: str,
        now_ms: int | None = None,
    ) -> tuple[str, SemanticMemory | None]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"""
                    SELECT {self._semantic_memory_columns()} FROM semantic_memories
                    WHERE workspace=? AND namespace=? AND memory_type=?
                      AND subject=? AND predicate=? AND status='active'
                    """,
                    (workspace, namespace, memory_type, subject, predicate),
                )
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
                if not row:
                    await db.rollback()
                    return "NOOP", None
                current = self._semantic_memory_from_row(row)
                await db.execute(
                    "UPDATE semantic_memories SET status='deleted', valid_to=?, updated_at=? WHERE id=?",
                    (now, now, current.id),
                )
                await db.execute("DELETE FROM semantic_memories_fts WHERE memory_id=?", (current.id,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return "DELETE", await self.get_semantic_memory(current.id)

    async def get_semantic_memory(self, memory_id: str) -> SemanticMemory:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"SELECT {self._semantic_memory_columns()} FROM semantic_memories WHERE id=?",
                (memory_id,),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if not row:
            raise KeyError(f"semantic memory not found: {memory_id}")
        return self._semantic_memory_from_row(row)

    async def list_semantic_memories(
        self,
        workspace: str,
        *,
        namespace: str = "project",
        status: str | None = "active",
        limit: int = 100,
        offset: int = 0,
    ) -> list[SemanticMemory]:
        conditions = ["workspace=?", "namespace=?"]
        params: list[Any] = [workspace, namespace]
        if status is not None:
            conditions.append("status=?")
            params.append(status)
        params.extend([max(1, limit), max(0, offset)])
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"""
                SELECT {self._semantic_memory_columns()} FROM semantic_memories
                WHERE {' AND '.join(conditions)}
                ORDER BY importance DESC, updated_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._semantic_memory_from_row(row) for row in rows]

    async def search_semantic_memories(
        self,
        workspace: str,
        query: str,
        *,
        namespace: str = "project",
        limit: int = 5,
    ) -> list[SemanticMemoryHit]:
        fts_query = self._build_fts_query(query, operator="OR", expand_cjk=True)
        if not fts_query:
            return []
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"""
                SELECT {', '.join(f'm.{column}' for column in self._semantic_memory_columns().split(', '))},
                       bm25(semantic_memories_fts) AS rank
                FROM semantic_memories_fts
                JOIN semantic_memories m ON m.id=semantic_memories_fts.memory_id
                WHERE semantic_memories_fts MATCH ?
                  AND m.workspace=? AND m.namespace=? AND m.status='active'
                ORDER BY rank ASC, m.importance DESC
                LIMIT ?
                """,
                (fts_query, workspace, namespace, max(1, limit)),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [
            SemanticMemoryHit(
                memory=self._semantic_memory_from_row(row[:22]),
                score=1.0 / (1.0 + abs(float(row[22] or 0))),
            )
            for row in rows
        ]

    async def delete_semantic_memory(self, memory_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("DELETE FROM semantic_memories_fts WHERE memory_id=?", (memory_id,))
                await db.execute("DELETE FROM semantic_memories WHERE id=?", (memory_id,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def forget_semantic_memory(
        self,
        memory_id: str,
        *,
        now_ms: int | None = None,
    ) -> SemanticMemory:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("SELECT status FROM semantic_memories WHERE id=?", (memory_id,))
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
                if not row:
                    raise KeyError(f"semantic memory not found: {memory_id}")
                if row[0] != "deleted":
                    await db.execute(
                        """
                        UPDATE semantic_memories
                        SET status='deleted', valid_to=COALESCE(valid_to, ?), updated_at=?
                        WHERE id=?
                        """,
                        (now, now, memory_id),
                    )
                    await db.execute("DELETE FROM semantic_memories_fts WHERE memory_id=?", (memory_id,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_semantic_memory(memory_id)

    async def get_memory_processing_status(self) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            outbox_cursor = await db.execute(
                "SELECT status, COUNT(*) FROM outbox_events GROUP BY status"
            )
            try:
                outbox_rows = await outbox_cursor.fetchall()
            finally:
                await outbox_cursor.close()
            checkpoint_cursor = await db.execute(
                """
                SELECT processor_name, COUNT(*), MAX(updated_at)
                FROM memory_checkpoints GROUP BY processor_name
                """
            )
            try:
                checkpoint_rows = await checkpoint_cursor.fetchall()
            finally:
                await checkpoint_cursor.close()
        return {
            "outbox": {str(row[0]): int(row[1]) for row in outbox_rows},
            "checkpoints": {
                str(row[0]): {"sessions": int(row[1]), "last_updated_at": int(row[2] or 0)}
                for row in checkpoint_rows
            },
        }

    async def replace_core_memory_blocks(
        self,
        workspace: str,
        namespace: str,
        blocks: list[CoreMemoryBlock],
    ) -> list[CoreMemoryBlock]:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                keep_types: list[str] = []
                for block in blocks:
                    keep_types.append(block.block_type)
                    cursor = await db.execute(
                        """
                        SELECT content_hash, version, created_at FROM core_memory_blocks
                        WHERE workspace=? AND namespace=? AND block_type=?
                        """,
                        (workspace, namespace, block.block_type),
                    )
                    try:
                        current = await cursor.fetchone()
                    finally:
                        await cursor.close()
                    if current and current[0] == block.content_hash:
                        version = int(current[1])
                    elif current:
                        version = int(current[1]) + 1
                    else:
                        version = 1
                    created_at = int(current[2]) if current else block.created_at
                    await db.execute(
                        """
                        INSERT INTO core_memory_blocks (
                          id, workspace, namespace, block_type, content,
                          source_memory_ids_json, priority, token_count,
                          content_hash, version, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(workspace, namespace, block_type) DO UPDATE SET
                          content=excluded.content,
                          source_memory_ids_json=excluded.source_memory_ids_json,
                          priority=excluded.priority,
                          token_count=excluded.token_count,
                          content_hash=excluded.content_hash,
                          version=excluded.version,
                          updated_at=excluded.updated_at
                        """,
                        (
                            block.id,
                            workspace,
                            namespace,
                            block.block_type,
                            block.content,
                            json.dumps(block.source_memory_ids),
                            block.priority,
                            block.token_count,
                            block.content_hash,
                            version,
                            created_at,
                            block.updated_at,
                        ),
                    )
                if keep_types:
                    placeholders = ",".join("?" for _ in keep_types)
                    await db.execute(
                        f"""
                        DELETE FROM core_memory_blocks
                        WHERE workspace=? AND namespace=? AND block_type NOT IN ({placeholders})
                        """,
                        (workspace, namespace, *keep_types),
                    )
                else:
                    await db.execute(
                        "DELETE FROM core_memory_blocks WHERE workspace=? AND namespace=?",
                        (workspace, namespace),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.list_core_memory_blocks(workspace, namespace=namespace)

    async def list_core_memory_blocks(
        self,
        workspace: str,
        *,
        namespace: str = "project",
    ) -> list[CoreMemoryBlock]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id, workspace, namespace, block_type, content,
                       source_memory_ids_json, priority, token_count,
                       content_hash, version, created_at, updated_at
                FROM core_memory_blocks
                WHERE workspace=? AND namespace=?
                ORDER BY priority DESC, block_type ASC
                """,
                (workspace, namespace),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._core_memory_block_from_row(row) for row in rows]

    async def create_run(
        self,
        *,
        session_id: str,
        trigger_message_id: str | None,
        source: str,
        agent_name: str,
        model: ModelRef,
        max_attempts: int = 1,
        now_ms: int | None = None,
    ) -> Run:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        run_id = str(uuid4())
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                session_cursor = await db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,))
                try:
                    session_exists = await session_cursor.fetchone()
                finally:
                    await session_cursor.close()
                if not session_exists:
                    raise KeyError(f"session not found: {session_id}")

                sequence_cursor = await db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM runs WHERE session_id=?",
                    (session_id,),
                )
                try:
                    sequence_row = await sequence_cursor.fetchone()
                finally:
                    await sequence_cursor.close()
                sequence = int(sequence_row[0]) if sequence_row else 1

                await db.execute(
                    """
                    INSERT INTO runs (
                      id, session_id, sequence, trigger_message_id, assistant_message_id,
                      status, source, agent_name, model_provider, model_id,
                      started_at, completed_at, finish_reason, error_json,
                      model_rounds, tool_call_count, input_sequence, output_sequence,
                      revision, state_reason, owner_id, lease_until, heartbeat_at,
                      attempt, max_attempts, next_attempt_at, checkpoint_seq,
                      checkpoint_json, cancel_requested_at, error_code, error_summary,
                      created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        session_id,
                        sequence,
                        trigger_message_id,
                        None,
                        "queued",
                        source,
                        agent_name,
                        model.provider,
                        model.id,
                        None,
                        None,
                        None,
                        None,
                        0,
                        0,
                        None,
                        None,
                        0,
                        "created",
                        None,
                        None,
                        None,
                        0,
                        max(1, max_attempts),
                        None,
                        0,
                        None,
                        None,
                        None,
                        None,
                        now,
                        now,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_run(run_id)

    async def get_run(self, run_id: str) -> Run:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT
                  id, session_id, sequence, trigger_message_id, assistant_message_id,
                  status, source, agent_name, model_provider, model_id,
                  started_at, completed_at, finish_reason, error_json,
                  model_rounds, tool_call_count, input_sequence, output_sequence,
                  revision, state_reason, owner_id, lease_until, heartbeat_at,
                  attempt, max_attempts, next_attempt_at, checkpoint_seq,
                  checkpoint_json, cancel_requested_at, error_code, error_summary,
                  created_at, updated_at
                FROM runs WHERE id=?
                """,
                (run_id,),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if not row:
            raise KeyError(f"run not found: {run_id}")
        return self._run_from_row(row)

    async def get_run_by_assistant_message(self, assistant_message_id: str) -> Run:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT
                  id, session_id, sequence, trigger_message_id, assistant_message_id,
                  status, source, agent_name, model_provider, model_id,
                  started_at, completed_at, finish_reason, error_json,
                  model_rounds, tool_call_count, input_sequence, output_sequence,
                  revision, state_reason, owner_id, lease_until, heartbeat_at,
                  attempt, max_attempts, next_attempt_at, checkpoint_seq,
                  checkpoint_json, cancel_requested_at, error_code, error_summary,
                  created_at, updated_at
                FROM runs WHERE assistant_message_id=?
                """,
                (assistant_message_id,),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if not row:
            raise KeyError(f"run not found for assistant message: {assistant_message_id}")
        return self._run_from_row(row)

    async def list_runs(self, session_id: str, *, limit: int = 50, offset: int = 0) -> list[Run]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT
                  id, session_id, sequence, trigger_message_id, assistant_message_id,
                  status, source, agent_name, model_provider, model_id,
                  started_at, completed_at, finish_reason, error_json,
                  model_rounds, tool_call_count, input_sequence, output_sequence,
                  revision, state_reason, owner_id, lease_until, heartbeat_at,
                  attempt, max_attempts, next_attempt_at, checkpoint_seq,
                  checkpoint_json, cancel_requested_at, error_code, error_summary,
                  created_at, updated_at
                FROM runs
                WHERE session_id=?
                ORDER BY sequence DESC
                LIMIT ? OFFSET ?
                """,
                (session_id, max(1, limit), max(0, offset)),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._run_from_row(row) for row in rows]

    @staticmethod
    def _run_feedback_from_row(row: tuple[Any, ...]) -> RunFeedback:
        return RunFeedback(
            id=row[0],
            run_id=row[1],
            session_id=row[2],
            rating=row[3],
            error_types=json.loads(row[4]),
            comment_redacted=row[5],
            redaction_count=row[6],
            created_at=row[7],
        )

    @staticmethod
    def _eval_candidate_from_row(row: tuple[Any, ...]) -> EvalCandidate:
        return EvalCandidate(
            id=row[0],
            feedback_id=row[1],
            run_id=row[2],
            session_id=row[3],
            status=row[4],
            prompt_redacted=row[5],
            response_redacted=row[6],
            error_types=json.loads(row[7]),
            feedback_redacted=row[8],
            run_status=row[9],
            source_message_ids=json.loads(row[10]),
            redaction_count=row[11],
            reviewer_note=row[12],
            reviewed_at=row[13],
            created_at=row[14],
            updated_at=row[15],
        )

    async def create_run_feedback(
        self,
        feedback: RunFeedback,
        candidate: EvalCandidate | None,
    ) -> tuple[RunFeedback, EvalCandidate | None, bool]:
        """Atomically persist immutable feedback and its optional bad-case candidate."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT id,run_id,session_id,rating,error_types_json,
                           comment_redacted,redaction_count,created_at
                    FROM run_feedback WHERE run_id=?
                    """,
                    (feedback.run_id,),
                )
                try:
                    existing_row = await cursor.fetchone()
                finally:
                    await cursor.close()
                if existing_row is not None:
                    existing = self._run_feedback_from_row(existing_row)
                    same_payload = (
                        existing.rating == feedback.rating
                        and existing.error_types == feedback.error_types
                        and existing.comment_redacted == feedback.comment_redacted
                    )
                    if not same_payload:
                        raise ValueError("feedback has already been submitted for this run")
                    candidate_cursor = await db.execute(
                        """
                        SELECT id,feedback_id,run_id,session_id,status,prompt_redacted,
                               response_redacted,error_types_json,feedback_redacted,
                               run_status,source_message_ids_json,redaction_count,
                               reviewer_note,reviewed_at,created_at,updated_at
                        FROM eval_candidates WHERE feedback_id=?
                        """,
                        (existing.id,),
                    )
                    try:
                        candidate_row = await candidate_cursor.fetchone()
                    finally:
                        await candidate_cursor.close()
                    await db.commit()
                    return (
                        existing,
                        self._eval_candidate_from_row(candidate_row)
                        if candidate_row is not None
                        else None,
                        False,
                    )

                await db.execute(
                    """
                    INSERT INTO run_feedback (
                      id,run_id,session_id,rating,error_types_json,
                      comment_redacted,redaction_count,created_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        feedback.id,
                        feedback.run_id,
                        feedback.session_id,
                        feedback.rating,
                        json.dumps(feedback.error_types),
                        feedback.comment_redacted,
                        feedback.redaction_count,
                        feedback.created_at,
                    ),
                )
                if candidate is not None:
                    await db.execute(
                        """
                        INSERT INTO eval_candidates (
                          id,feedback_id,run_id,session_id,status,prompt_redacted,
                          response_redacted,error_types_json,feedback_redacted,
                          run_status,source_message_ids_json,redaction_count,
                          reviewer_note,reviewed_at,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            candidate.id,
                            candidate.feedback_id,
                            candidate.run_id,
                            candidate.session_id,
                            candidate.status,
                            candidate.prompt_redacted,
                            candidate.response_redacted,
                            json.dumps(candidate.error_types),
                            candidate.feedback_redacted,
                            candidate.run_status,
                            json.dumps(candidate.source_message_ids),
                            candidate.redaction_count,
                            candidate.reviewer_note,
                            candidate.reviewed_at,
                            candidate.created_at,
                            candidate.updated_at,
                        ),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return feedback, candidate, True

    async def get_run_feedback(self, run_id: str) -> RunFeedback | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id,run_id,session_id,rating,error_types_json,
                       comment_redacted,redaction_count,created_at
                FROM run_feedback WHERE run_id=?
                """,
                (run_id,),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        return self._run_feedback_from_row(row) if row is not None else None

    async def list_run_feedback(
        self,
        *,
        session_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RunFeedback]:
        conditions: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            conditions.append("session_id=?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend((max(1, min(limit, 500)), max(0, offset)))
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"""
                SELECT id,run_id,session_id,rating,error_types_json,
                       comment_redacted,redaction_count,created_at
                FROM run_feedback {where}
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                tuple(params),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._run_feedback_from_row(row) for row in rows]

    async def list_eval_candidates(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EvalCandidate]:
        if status is not None and status not in {"pending", "accepted", "rejected"}:
            raise ValueError(f"invalid eval candidate status: {status}")
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status=?")
            params.append(status)
        if session_id is not None:
            conditions.append("session_id=?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend((max(1, min(limit, 500)), max(0, offset)))
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"""
                SELECT id,feedback_id,run_id,session_id,status,prompt_redacted,
                       response_redacted,error_types_json,feedback_redacted,
                       run_status,source_message_ids_json,redaction_count,
                       reviewer_note,reviewed_at,created_at,updated_at
                FROM eval_candidates {where}
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                tuple(params),
            )
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [self._eval_candidate_from_row(row) for row in rows]

    async def get_eval_candidate(self, candidate_id: str) -> EvalCandidate:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id,feedback_id,run_id,session_id,status,prompt_redacted,
                       response_redacted,error_types_json,feedback_redacted,
                       run_status,source_message_ids_json,redaction_count,
                       reviewer_note,reviewed_at,created_at,updated_at
                FROM eval_candidates WHERE id=?
                """,
                (candidate_id,),
            )
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        if row is None:
            raise KeyError(f"eval candidate not found: {candidate_id}")
        return self._eval_candidate_from_row(row)

    async def review_eval_candidate(
        self,
        candidate_id: str,
        *,
        decision: str,
        note: str,
        now_ms: int | None = None,
    ) -> EvalCandidate:
        if decision not in {"accept", "reject"}:
            raise ValueError(f"invalid review decision: {decision}")
        target_status = "accepted" if decision == "accept" else "rejected"
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT status FROM eval_candidates WHERE id=?", (candidate_id,)
                )
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
                if row is None:
                    raise KeyError(f"eval candidate not found: {candidate_id}")
                current_status = str(row[0])
                if current_status == target_status:
                    await db.commit()
                elif current_status != "pending":
                    raise ValueError(
                        f"eval candidate is already reviewed as {current_status}"
                    )
                else:
                    await db.execute(
                        """
                        UPDATE eval_candidates
                        SET status=?, reviewer_note=?, reviewed_at=?, updated_at=?
                        WHERE id=? AND status='pending'
                        """,
                        (target_status, note, now, now, candidate_id),
                    )
                    await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_eval_candidate(candidate_id)

    async def start_run(
        self,
        run_id: str,
        *,
        input_sequence: int | None,
        owner_id: str | None = None,
        lease_duration_ms: int = 20_000,
    ) -> Run:
        now = int(time.time() * 1000)
        owner = owner_id or f"legacy:{run_id}"
        lease_until = now + max(1_000, lease_duration_ms)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                run_cursor = await db.execute(
                    "SELECT session_id, status, revision FROM runs WHERE id=?",
                    (run_id,),
                )
                run_row = await run_cursor.fetchone()
                await run_cursor.close()
                if not run_row:
                    raise KeyError(f"run not found: {run_id}")
                if str(run_row[1]) != "queued":
                    raise ValueError(f"run is not queued: {run_id}")
                session_id = str(run_row[0])
                revision = int(run_row[2])

                lease_cursor = await db.execute(
                    """
                    INSERT INTO session_leases (
                      session_id, run_id, owner_id, lease_until, revision, updated_at
                    ) VALUES (?,?,?,?,0,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                      run_id=excluded.run_id,
                      owner_id=excluded.owner_id,
                      lease_until=excluded.lease_until,
                      revision=session_leases.revision+1,
                      updated_at=excluded.updated_at
                    WHERE session_leases.lease_until<=?
                       OR session_leases.owner_id=excluded.owner_id
                    """,
                    (session_id, run_id, owner, lease_until, now, now),
                )
                if lease_cursor.rowcount == 0:
                    await lease_cursor.close()
                    raise RuntimeError(f"session already has an active run: {session_id}")
                await lease_cursor.close()

                cursor = await db.execute(
                    """
                    UPDATE runs
                    SET status='running', started_at=?, input_sequence=?, updated_at=?,
                        state_reason='worker_acquired', owner_id=?, lease_until=?,
                        heartbeat_at=?, attempt=attempt+1, revision=revision+1
                    WHERE id=? AND status='queued' AND revision=?
                    """,
                    (
                        now,
                        input_sequence,
                        now,
                        owner,
                        lease_until,
                        now,
                        run_id,
                        revision,
                    ),
                )
                if cursor.rowcount == 0:
                    await cursor.close()
                    raise RuntimeError(f"run state changed while acquiring: {run_id}")
                await cursor.close()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_run(run_id)

    async def heartbeat_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_duration_ms: int,
    ) -> bool:
        now = int(time.time() * 1000)
        lease_until = now + max(1_000, lease_duration_ms)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    UPDATE runs
                    SET heartbeat_at=?, lease_until=?, updated_at=?, revision=revision+1
                    WHERE id=? AND owner_id=?
                      AND status IN ('acquiring','running','waiting_approval','waiting_retry','recovery_pending','cancelling')
                    """,
                    (now, lease_until, now, run_id, owner_id),
                )
                updated = cursor.rowcount == 1
                await cursor.close()
                if updated:
                    await db.execute(
                        """
                        UPDATE session_leases
                        SET lease_until=?, updated_at=?, revision=revision+1
                        WHERE run_id=? AND owner_id=?
                        """,
                        (lease_until, now, run_id, owner_id),
                    )
                await db.commit()
                return updated
            except BaseException:
                await db.rollback()
                raise

    async def request_run_cancel(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        reason: str = "user_cancelled",
    ) -> Run | None:
        if not session_id and not run_id:
            raise ValueError("session_id or run_id is required")
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                if run_id:
                    cursor = await db.execute(
                        """
                        SELECT id,status FROM runs
                        WHERE id=? AND status IN ('queued','acquiring','running','waiting_approval','waiting_retry','recovery_pending','cancelling')
                        """,
                        (run_id,),
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT id,status FROM runs
                        WHERE session_id=?
                          AND status IN ('queued','acquiring','running','waiting_approval','waiting_retry','recovery_pending','cancelling')
                        ORDER BY sequence DESC LIMIT 1
                        """,
                        (session_id,),
                    )
                row = await cursor.fetchone()
                await cursor.close()
                if not row:
                    await db.commit()
                    return None
                target_id = str(row[0])
                if str(row[1]) == "cancelling":
                    await db.commit()
                    return await self.get_run(target_id)
                await db.execute(
                    """
                    UPDATE runs
                    SET status='cancelling', cancel_requested_at=COALESCE(cancel_requested_at,?),
                        state_reason=?, updated_at=?, revision=revision+1
                    WHERE id=? AND status NOT IN ('completed','failed','cancelled','interrupted')
                    """,
                    (now, reason, now, target_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_run(target_id)

    async def is_run_cancel_requested(self, run_id: str) -> bool:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT cancel_requested_at, status FROM runs WHERE id=?",
                (run_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return bool(row and (row[0] is not None or str(row[1]) == "cancelling"))

    async def set_run_approval_state(
        self,
        *,
        assistant_message_id: str,
        waiting: bool,
    ) -> Run | None:
        now = int(time.time() * 1000)
        target = "waiting_approval" if waiting else "running"
        source = "running" if waiting else "waiting_approval"
        reason = "approval_requested" if waiting else "approval_resolved"
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT run_id FROM messages WHERE id=? AND role='assistant'",
                (assistant_message_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if not row or not row[0]:
                return None
            run_id = str(row[0])
            update = await db.execute(
                """
                UPDATE runs
                SET status=?, state_reason=?, updated_at=?, revision=revision+1
                WHERE id=? AND status=?
                """,
                (target, reason, now, run_id, source),
            )
            changed = update.rowcount == 1
            await update.close()
            await db.commit()
        return await self.get_run(run_id) if changed else None

    async def checkpoint_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        checkpoint: dict[str, Any],
        state_reason: str,
    ) -> Run:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                UPDATE runs
                SET checkpoint_seq=checkpoint_seq+1, checkpoint_json=?,
                    state_reason=?, updated_at=?, revision=revision+1
                WHERE id=? AND owner_id=?
                  AND status IN ('running','waiting_approval','waiting_retry','cancelling')
                """,
                (
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
                    state_reason,
                    now,
                    run_id,
                    owner_id,
                ),
            )
            updated = cursor.rowcount == 1
            await cursor.close()
            await db.commit()
        if not updated:
            raise RuntimeError(f"run lease lost while checkpointing: {run_id}")
        return await self.get_run(run_id)

    async def create_run_step(
        self,
        *,
        run_id: str,
        kind: str,
        status: str = "running",
        input_ref: str | None = None,
        operation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        now_ms: int | None = None,
    ) -> RunStep:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        step_id = str(uuid4())
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                sequence_cursor = await db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM run_steps WHERE run_id=?",
                    (run_id,),
                )
                row = await sequence_cursor.fetchone()
                await sequence_cursor.close()
                sequence = int(row[0]) if row else 1
                await db.execute(
                    """
                    INSERT INTO run_steps (
                      id,run_id,sequence,kind,status,input_ref,output_ref,
                      operation_id,started_at,finished_at,error_code,metadata_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        step_id,
                        run_id,
                        sequence,
                        kind,
                        status,
                        input_ref,
                        None,
                        operation_id,
                        now,
                        None,
                        None,
                        json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_run_step(step_id)

    async def finish_run_step(
        self,
        step_id: str,
        *,
        status: str,
        output_ref: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
        now_ms: int | None = None,
    ) -> RunStep:
        if status not in {"completed", "failed", "cancelled", "interrupted", "needs_review"}:
            raise ValueError(f"invalid terminal step status: {status}")
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                UPDATE run_steps
                SET status=?, output_ref=?, finished_at=?, error_code=?, metadata_json=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    output_ref,
                    now,
                    error_code,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    step_id,
                ),
            )
            updated = cursor.rowcount == 1
            await cursor.close()
            await db.commit()
        if not updated:
            existing = await self.get_run_step(step_id)
            if existing.status != status:
                raise ValueError(f"run step is already terminal: {step_id}")
            return existing
        return await self.get_run_step(step_id)

    async def get_run_step(self, step_id: str) -> RunStep:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id,run_id,sequence,kind,status,input_ref,output_ref,
                       operation_id,started_at,finished_at,error_code,metadata_json
                FROM run_steps WHERE id=?
                """,
                (step_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            raise KeyError(f"run step not found: {step_id}")
        return self._run_step_from_row(row)

    async def list_run_steps(self, run_id: str) -> list[RunStep]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT id,run_id,sequence,kind,status,input_ref,output_ref,
                       operation_id,started_at,finished_at,error_code,metadata_json
                FROM run_steps WHERE run_id=? ORDER BY sequence ASC
                """,
                (run_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._run_step_from_row(row) for row in rows]

    @staticmethod
    def _run_step_from_row(row: tuple[Any, ...]) -> RunStep:
        return RunStep(
            id=row[0],
            run_id=row[1],
            sequence=row[2],
            kind=row[3],
            status=row[4],
            input_ref=row[5],
            output_ref=row[6],
            operation_id=row[7],
            started_at=row[8],
            finished_at=row[9],
            error_code=row[10],
            metadata=json.loads(row[11]) if row[11] else {},
        )

    async def reconcile_abandoned_runs(
        self,
        *,
        owner_id: str,
        stale_after_ms: int = 20_000,
        now_ms: int | None = None,
    ) -> list[Run]:
        """Conservatively interrupt non-terminal Runs left by another process.

        A fresh process cannot prove whether an in-flight tool produced side
        effects. It therefore records an explicit terminal error instead of
        replaying the operation.
        """
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        terminalized_ids: list[str] = []
        active_states = (
            "queued",
            "acquiring",
            "running",
            "waiting_approval",
            "waiting_retry",
            "recovery_pending",
            "cancelling",
        )
        placeholders = ",".join("?" for _ in active_states)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"""
                    SELECT id,session_id,status,owner_id,revision,input_sequence
                    FROM runs
                    WHERE status IN ({placeholders})
                      AND (owner_id IS NULL OR owner_id<>?)
                      AND (
                        (owner_id IS NULL AND updated_at<=?)
                        OR (lease_until IS NOT NULL AND lease_until<=?)
                      )
                    ORDER BY created_at ASC
                    """,
                    (*active_states, owner_id, now - max(1_000, stale_after_ms), now),
                )
                rows = await cursor.fetchall()
                await cursor.close()
                for run_id_raw, session_id_raw, old_status_raw, _old_owner, revision_raw, input_sequence in rows:
                    run_id = str(run_id_raw)
                    session_id = str(session_id_raw)
                    old_status = str(old_status_raw)
                    revision = int(revision_raw)
                    error_code = "runtime_restarted"
                    error_message = (
                        "Run was interrupted because its original worker is no longer active. "
                        "In-flight tool side effects were not replayed."
                    )

                    message_cursor = await db.execute(
                        "SELECT id FROM messages WHERE run_id=? AND role='assistant' ORDER BY sequence DESC LIMIT 1",
                        (run_id,),
                    )
                    message_row = await message_cursor.fetchone()
                    await message_cursor.close()
                    assistant_message_id = str(message_row[0]) if message_row else None

                    part_cursor = await db.execute(
                        """
                        SELECT p.content_json
                        FROM parts p
                        JOIN messages m ON m.id=p.message_id
                        WHERE m.run_id=? AND p.type='tool'
                        ORDER BY p.id ASC
                        """,
                        (run_id,),
                    )
                    part_rows = await part_cursor.fetchall()
                    await part_cursor.close()
                    latest_parts: dict[str, dict[str, Any]] = {}
                    for (content_json,) in part_rows:
                        try:
                            content = json.loads(content_json)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        part_id = str(content.get("id", ""))
                        if part_id:
                            latest_parts[part_id] = content
                    for content in latest_parts.values():
                        state = content.get("state") or {}
                        if state.get("status") not in {"pending", "running"}:
                            continue
                        start = ((state.get("time") or {}).get("start")) or now
                        error_part = dict(content)
                        error_part["state"] = {
                            "status": "error",
                            "input": state.get("input") or {},
                            "error": error_message,
                            "metadata": {
                                "error_code": error_code,
                                "previous_status": state.get("status"),
                                "side_effect_state": "unknown"
                                if state.get("status") == "running"
                                else "none",
                            },
                            "time": {"start": int(start), "end": now},
                        }
                        await db.execute(
                            "INSERT INTO parts (session_id,message_id,type,content_json,created_at) VALUES (?,?,?,?,?)",
                            (
                                session_id,
                                str(content.get("message_id") or assistant_message_id or ""),
                                "tool",
                                json.dumps(error_part, ensure_ascii=False),
                                now,
                            ),
                        )

                    error_payload = {
                        "code": error_code,
                        "message": error_message,
                        "previous_status": old_status,
                    }
                    if assistant_message_id:
                        await db.execute(
                            """
                            UPDATE messages
                            SET completed_at=COALESCE(completed_at,?),
                                finish=COALESCE(finish,'interrupted'),
                                error_json=COALESCE(error_json,?)
                            WHERE id=?
                            """,
                            (
                                now,
                                json.dumps(error_payload, ensure_ascii=False),
                                assistant_message_id,
                            ),
                        )
                    output_cursor = await db.execute(
                        "SELECT MAX(sequence) FROM messages WHERE run_id=?",
                        (run_id,),
                    )
                    output_row = await output_cursor.fetchone()
                    await output_cursor.close()
                    output_sequence = (
                        int(output_row[0])
                        if output_row and output_row[0] is not None
                        else None
                    )
                    update_cursor = await db.execute(
                        """
                        UPDATE runs
                        SET status='interrupted', assistant_message_id=?, completed_at=?,
                            finish_reason='runtime_restarted', error_json=?,
                            output_sequence=?, state_reason='startup_reconciliation',
                            owner_id=NULL, lease_until=NULL, heartbeat_at=COALESCE(heartbeat_at,updated_at),
                            error_code=?, error_summary=?, updated_at=?, revision=revision+1
                        WHERE id=? AND revision=? AND status=?
                        """,
                        (
                            assistant_message_id,
                            now,
                            json.dumps(error_payload, ensure_ascii=False),
                            output_sequence,
                            error_code,
                            error_message,
                            now,
                            run_id,
                            revision,
                            old_status,
                        ),
                    )
                    changed = update_cursor.rowcount == 1
                    await update_cursor.close()
                    if not changed:
                        continue
                    await db.execute("DELETE FROM session_leases WHERE run_id=?", (run_id,))
                    await db.execute(
                        "UPDATE permission_requests SET status='interrupted' WHERE session_id=? AND status='pending'",
                        (session_id,),
                    )
                    await db.execute(
                        """
                        UPDATE run_steps
                        SET status='interrupted', finished_at=?, error_code=?
                        WHERE run_id=? AND status='running'
                        """,
                        (now, error_code, run_id),
                    )
                    await db.execute(
                        """
                        UPDATE llm_call_attempts
                        SET status='interrupted', finished_at=?, error_code=?
                        WHERE call_id IN (SELECT id FROM llm_calls WHERE run_id=?)
                          AND status='running'
                        """,
                        (now, error_code, run_id),
                    )
                    await db.execute(
                        """
                        UPDATE llm_calls
                        SET status='interrupted', finished_at=?, error_code=?,
                            public_error=?
                        WHERE run_id=? AND status IN ('planned','running','retrying')
                        """,
                        (now, error_code, error_message, run_id),
                    )
                    operation_cursor = await db.execute(
                        """
                        SELECT operation_id,result_json
                        FROM tool_operations
                        WHERE run_id=? AND status='executing'
                        """,
                        (run_id,),
                    )
                    executing_operations = await operation_cursor.fetchall()
                    await operation_cursor.close()
                    for operation_id, result_json in executing_operations:
                        try:
                            previous_result = json.loads(result_json) if result_json else {}
                        except (TypeError, json.JSONDecodeError):
                            previous_result = {}
                        previous_metadata = previous_result.get("metadata")
                        metadata = (
                            dict(previous_metadata)
                            if isinstance(previous_metadata, dict)
                            else {}
                        )
                        recovery_action = str(
                            metadata.get("recovery_action") or "manual_review"
                        )
                        side_effect_state = str(
                            metadata.get("side_effect_state") or "unknown"
                        )
                        operation_status = (
                            "interrupted"
                            if recovery_action in {"safe_to_retry", "retry_with_same_key"}
                            else "needs_review"
                        )
                        recovery_result = {
                            "metadata": {
                                **metadata,
                                "side_effect_state": side_effect_state,
                                "recovery_action": recovery_action,
                                "automatic_replay": False,
                                "error_code": error_code,
                            },
                            "output": error_message,
                        }
                        await db.execute(
                            """
                            UPDATE tool_operations
                            SET status=?, error_code=?, result_json=?, finished_at=?
                            WHERE operation_id=? AND status='executing'
                            """,
                            (
                                operation_status,
                                error_code,
                                json.dumps(recovery_result, ensure_ascii=False),
                                now,
                                operation_id,
                            ),
                        )
                    event_id = str(uuid4())
                    payload = {
                        "run_id": run_id,
                        "session_id": session_id,
                        "status": "interrupted",
                        "finish_reason": "runtime_restarted",
                        "assistant_message_id": assistant_message_id,
                    }
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO outbox_events (
                          id,idempotency_key,event_type,aggregate_type,aggregate_id,
                          session_id,run_id,sequence_from,sequence_to,payload_json,
                          status,attempts,created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            event_id,
                            f"run.completed:{run_id}",
                            "run.completed",
                            "run",
                            run_id,
                            session_id,
                            run_id,
                            input_sequence,
                            output_sequence,
                            json.dumps(payload, sort_keys=True),
                            "pending",
                            0,
                            now,
                        ),
                    )
                    terminalized_ids.append(run_id)
                await db.execute("DELETE FROM session_leases WHERE lease_until<=?", (now,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return [await self.get_run(run_id) for run_id in terminalized_ids]

    async def attach_message_to_run(self, message_id: str, run_id: str) -> int:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                UPDATE messages
                SET run_id=?
                WHERE id=?
                  AND session_id=(SELECT session_id FROM runs WHERE id=?)
                  AND (run_id IS NULL OR run_id=?)
                """,
                (run_id, message_id, run_id, run_id),
            )
            if cursor.rowcount == 0:
                await cursor.close()
                raise ValueError("message cannot be attached to the requested run")
            await cursor.close()
            sequence_cursor = await db.execute("SELECT sequence FROM messages WHERE id=?", (message_id,))
            try:
                sequence_row = await sequence_cursor.fetchone()
            finally:
                await sequence_cursor.close()
            await db.commit()
        if not sequence_row:
            raise KeyError(f"message not found: {message_id}")
        return int(sequence_row[0])

    async def increment_run_model_rounds(self, run_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE runs SET model_rounds=model_rounds+1, updated_at=? WHERE id=? AND status='running'",
                (int(time.time() * 1000), run_id),
            )
            await db.commit()

    async def increment_run_tool_calls(self, run_id: str, count: int) -> None:
        if count <= 0:
            return
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE runs SET tool_call_count=tool_call_count+?, updated_at=? WHERE id=? AND status='running'",
                (count, int(time.time() * 1000), run_id),
            )
            await db.commit()

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        assistant_message_id: str | None,
        finish_reason: str,
        error: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> Run:
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError(f"invalid terminal run status: {status}")
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            sequence_cursor = await db.execute(
                "SELECT MAX(sequence) FROM messages WHERE run_id=?",
                (run_id,),
            )
            try:
                sequence_row = await sequence_cursor.fetchone()
            finally:
                await sequence_cursor.close()
            output_sequence = int(sequence_row[0]) if sequence_row and sequence_row[0] is not None else None
            cursor = await db.execute(
                """
                UPDATE runs
                SET status=?, assistant_message_id=?, completed_at=?, finish_reason=?,
                    error_json=?, output_sequence=?, updated_at=?, state_reason=?,
                    error_code=?, error_summary=?, owner_id=NULL, lease_until=NULL,
                    revision=revision+1
                WHERE id=? AND status IN (
                  'queued','acquiring','running','waiting_approval','waiting_retry',
                  'recovery_pending','cancelling'
                )
                """,
                (
                    status,
                    assistant_message_id,
                    now,
                    finish_reason,
                    json.dumps(error) if error else None,
                    output_sequence,
                    now,
                    finish_reason,
                    error_code,
                    str((error or {}).get("message", ""))[:1000] or None,
                    run_id,
                ),
            )
            unchanged = cursor.rowcount == 0
            await cursor.close()
            if not unchanged:
                await db.execute("DELETE FROM session_leases WHERE run_id=?", (run_id,))
            await db.commit()
        return await self.get_run(run_id)

    async def finish_assistant_run_with_outbox(
        self,
        *,
        assistant_message_id: str,
        completed_at: int,
        finish_reason: str,
        run_status: str,
        error: dict[str, Any] | None = None,
    ) -> tuple[Run, OutboxEvent]:
        if run_status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError(f"invalid terminal run status: {run_status}")
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                message_cursor = await db.execute(
                    "SELECT session_id, run_id FROM messages WHERE id=? AND role='assistant'",
                    (assistant_message_id,),
                )
                try:
                    message_row = await message_cursor.fetchone()
                finally:
                    await message_cursor.close()
                if not message_row or not message_row[1]:
                    raise ValueError("assistant message is not attached to a run")
                session_id = str(message_row[0])
                run_id = str(message_row[1])

                run_cursor = await db.execute(
                    "SELECT input_sequence, status FROM runs WHERE id=? AND session_id=?",
                    (run_id, session_id),
                )
                try:
                    run_row = await run_cursor.fetchone()
                finally:
                    await run_cursor.close()
                if not run_row:
                    raise KeyError(f"run not found: {run_id}")
                if str(run_row[1]) not in {
                    "queued",
                    "acquiring",
                    "running",
                    "waiting_approval",
                    "waiting_retry",
                    "recovery_pending",
                    "cancelling",
                }:
                    raise ValueError(f"run is already terminal: {run_id}")
                input_sequence = int(run_row[0]) if run_row[0] is not None else None

                sequence_cursor = await db.execute("SELECT MAX(sequence) FROM messages WHERE run_id=?", (run_id,))
                try:
                    sequence_row = await sequence_cursor.fetchone()
                finally:
                    await sequence_cursor.close()
                output_sequence = int(sequence_row[0]) if sequence_row and sequence_row[0] is not None else None
                error_json = json.dumps(error) if error else None

                await db.execute(
                    """
                    UPDATE messages
                    SET completed_at=?, finish=?, error_json=?
                    WHERE id=?
                    """,
                    (completed_at, finish_reason, error_json, assistant_message_id),
                )
                await db.execute(
                    """
                    UPDATE runs
                    SET status=?, assistant_message_id=?, completed_at=?, finish_reason=?,
                        error_json=?, output_sequence=?, updated_at=?, state_reason=?,
                        error_code=?, error_summary=?, owner_id=NULL, lease_until=NULL,
                        revision=revision+1
                    WHERE id=?
                    """,
                    (
                        run_status,
                        assistant_message_id,
                        completed_at,
                        finish_reason,
                        error_json,
                        output_sequence,
                        completed_at,
                        finish_reason,
                        str((error or {}).get("code", "")) or None,
                        str((error or {}).get("message", ""))[:1000] or None,
                        run_id,
                    ),
                )
                await db.execute("DELETE FROM session_leases WHERE run_id=?", (run_id,))

                event_id = str(uuid4())
                payload = {
                    "run_id": run_id,
                    "session_id": session_id,
                    "status": run_status,
                    "finish_reason": finish_reason,
                    "assistant_message_id": assistant_message_id,
                }
                await db.execute(
                    """
                    INSERT INTO outbox_events (
                      id, idempotency_key, event_type, aggregate_type, aggregate_id,
                      session_id, run_id, sequence_from, sequence_to, payload_json,
                      status, attempts, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event_id,
                        f"run.completed:{run_id}",
                        "run.completed",
                        "run",
                        run_id,
                        session_id,
                        run_id,
                        input_sequence,
                        output_sequence,
                        json.dumps(payload, sort_keys=True),
                        "pending",
                        0,
                        completed_at,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_run(run_id), await self.get_outbox_event(event_id)

    async def get_session(self, session_id: str) -> Session:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT
                  id,title,worktree,cwd,created_at,updated_at,permission_rules_json,
                  kind,agent_name,root_session_id,parent_session_id,parent_tool_call_id,
                  runtime_backend,runtime_json,project_worktree,project_id
                FROM sessions WHERE id=?
                """,
                (session_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise KeyError(f"session not found: {session_id}")
            return self._session_from_row(row)

    async def touch_session(self, session_id: str) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            await db.execute(
                """
                UPDATE projects SET last_opened_at=?, updated_at=?
                WHERE id=(SELECT project_id FROM sessions WHERE id=?)
                """,
                (now, now, session_id),
            )
            await db.commit()

    async def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        include_children: bool = True,
        parent_session_id: str | None = None,
        project_id: str | None = None,
    ) -> list[Session]:
        """List sessions ordered by updated_at desc."""
        async with aiosqlite.connect(self._path) as db:
            where: list[str] = []
            params: list[Any] = []
            if parent_session_id is not None:
                where.append("parent_session_id = ?")
                params.append(parent_session_id)
            elif not include_children:
                where.append("(parent_session_id IS NULL OR parent_session_id = '')")
            if project_id is not None:
                where.append("project_id = ?")
                params.append(project_id)
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            cur = await db.execute(
                f"""
                SELECT
                  id,title,worktree,cwd,created_at,updated_at,permission_rules_json,
                  kind,agent_name,root_session_id,parent_session_id,parent_tool_call_id,
                  runtime_backend,runtime_json,project_worktree,project_id
                FROM sessions
                {clause}
                ORDER BY updated_at DESC LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            )
            rows = await cur.fetchall()
            out: list[Session] = []
            for row in rows:
                out.append(self._session_from_row(row))
            return out

    async def update_session_title(self, session_id: str, title: str) -> Session:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, now, session_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"session not found: {session_id}")
            await db.commit()
        return await self.get_session(session_id)

    async def update_session_permission_rules(self, session_id: str, rules: list[PermissionRule]) -> Session:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "UPDATE sessions SET permission_rules_json=?, updated_at=? WHERE id=?",
                (json.dumps([r.model_dump() for r in rules]), now, session_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"session not found: {session_id}")
            await db.commit()
        return await self.get_session(session_id)

    async def update_session_runtime(self, session_id: str, runtime_backend: str, runtime_json: dict[str, Any]) -> Session:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "UPDATE sessions SET runtime_backend=?, runtime_json=?, updated_at=? WHERE id=?",
                (runtime_backend, json.dumps(runtime_json), now, session_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"session not found: {session_id}")
            await db.commit()
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,))
            if not await cur.fetchone():
                raise KeyError(f"session not found: {session_id}")

            # Cascade delete related rows owned by the session.
            await db.execute("DELETE FROM parts WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM channel_sessions WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM permission_requests WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM permission_approvals WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM todos WHERE session_id=?", (session_id,))
            await db.execute(
                "DELETE FROM plan_task_dependencies WHERE task_id IN "
                "(SELECT t.id FROM plan_tasks t JOIN task_plans p ON p.id=t.plan_id "
                "JOIN goals g ON g.id=p.goal_id WHERE g.session_id=?)",
                (session_id,),
            )
            await db.execute(
                "DELETE FROM plan_tasks WHERE plan_id IN "
                "(SELECT p.id FROM task_plans p JOIN goals g ON g.id=p.goal_id WHERE g.session_id=?)",
                (session_id,),
            )
            await db.execute(
                "DELETE FROM task_plans WHERE goal_id IN (SELECT id FROM goals WHERE session_id=?)",
                (session_id,),
            )
            await db.execute("DELETE FROM task_runtime_events WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM goals WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM cron_job_runs WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM cron_jobs WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM outbox_events WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM session_leases WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM tool_operations WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM artifacts WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM eval_candidates WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM run_feedback WHERE session_id=?", (session_id,))
            await db.execute(
                "DELETE FROM llm_call_attempts WHERE call_id IN "
                "(SELECT id FROM llm_calls WHERE run_id IN "
                "(SELECT id FROM runs WHERE session_id=?))",
                (session_id,),
            )
            await db.execute(
                "DELETE FROM llm_calls WHERE run_id IN (SELECT id FROM runs WHERE session_id=?)",
                (session_id,),
            )
            await db.execute(
                "DELETE FROM run_steps WHERE run_id IN (SELECT id FROM runs WHERE session_id=?)",
                (session_id,),
            )
            await db.execute("DELETE FROM memory_checkpoints WHERE session_id=?", (session_id,))
            await db.execute(
                "DELETE FROM memory_episodes_fts WHERE episode_id IN "
                "(SELECT id FROM memory_episodes WHERE source_session_id=?)",
                (session_id,),
            )
            await db.execute("DELETE FROM memory_episodes WHERE source_session_id=?", (session_id,))
            await db.execute(
                "DELETE FROM semantic_memories_fts WHERE memory_id IN "
                "(SELECT id FROM semantic_memories WHERE source_session_id=?)",
                (session_id,),
            )
            await db.execute("DELETE FROM semantic_memories WHERE source_session_id=?", (session_id,))
            await db.execute("DELETE FROM runs WHERE session_id=?", (session_id,))
            await db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            await db.commit()

    async def get_channel_session(self, channel: str, chat_id: str) -> str | None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT session_id FROM channel_sessions WHERE channel=? AND chat_id=?",
                (channel, chat_id),
            )
            row = await cur.fetchone()
            return str(row[0]) if row else None

    async def bind_channel_session(
        self,
        *,
        channel: str,
        chat_id: str,
        session_id: str,
        sender_id: str | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO channel_sessions(channel, chat_id, session_id, sender_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, chat_id)
                DO UPDATE SET
                  session_id=excluded.session_id,
                  sender_id=excluded.sender_id,
                  updated_at=excluded.updated_at
                """,
                (channel, chat_id, session_id, sender_id, now, now),
            )
            await db.commit()

    async def add_message(self, message: Message) -> Message:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                if message.run_id is not None:
                    run_cursor = await db.execute(
                        "SELECT session_id FROM runs WHERE id=?",
                        (message.run_id,),
                    )
                    try:
                        run_row = await run_cursor.fetchone()
                    finally:
                        await run_cursor.close()
                    if not run_row:
                        raise KeyError(f"run not found: {message.run_id}")
                    if str(run_row[0]) != message.session_id:
                        raise ValueError("message and run must belong to the same session")

                sequence = message.sequence
                if sequence is None:
                    sequence_cursor = await db.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_id=?",
                        (message.session_id,),
                    )
                    try:
                        sequence_row = await sequence_cursor.fetchone()
                    finally:
                        await sequence_cursor.close()
                    sequence = int(sequence_row[0]) if sequence_row else 1
                if sequence <= 0:
                    raise ValueError("message sequence must be positive")

                await db.execute(
                    """
                    INSERT INTO messages (
                      id, session_id, run_id, sequence, role, parent_id, agent,
                      model_provider, model_id, created_at, completed_at, finish,
                      error_json, tool_call_id, tool_name
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        message.id,
                        message.session_id,
                        message.run_id,
                        sequence,
                        message.role,
                        message.parent_id,
                        message.agent,
                        message.model.provider,
                        message.model.id,
                        message.created_at,
                        message.completed_at,
                        message.finish,
                        json.dumps(message.error) if message.error else None,
                        message.tool_call_id,
                        message.tool_name,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return message.model_copy(update={"sequence": sequence})

    async def update_message(
        self,
        message_id: str,
        *,
        completed_at: Optional[int] = None,
        finish: Optional[str] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE messages
                SET
                  completed_at=COALESCE(?,completed_at),
                  finish=COALESCE(?,finish),
                  error_json=COALESCE(?,error_json)
                WHERE id=?
                """,
                (completed_at, finish, json.dumps(error) if error else None, message_id),
            )
            await db.commit()

    async def add_part(self, session_id: str, message_id: str, part: Part) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO parts (session_id,message_id,type,content_json,created_at) VALUES (?,?,?,?,?)",
                (
                    session_id,
                    message_id,
                    part.type,
                    json.dumps(part.model_dump()),
                    int(time.time() * 1000),
                ),
            )
            await db.commit()

    async def quarantine_provider_state(self, run_id: str) -> int:
        """Persistently exclude opaque provider state rejected for one Run."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                UPDATE parts
                SET content_json=json_set(
                    content_json,
                    '$.data._nexa_rejected',
                    json('true')
                )
                WHERE type='provider_state'
                  AND message_id IN (SELECT id FROM messages WHERE run_id=?)
                """,
                (run_id,),
            )
            count = int(cursor.rowcount or 0)
            await cursor.close()
            await db.commit()
        return count

    async def list_messages(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        after_sequence: int | None = None,
        through_sequence: int | None = None,
    ) -> list[MessageWithParts]:
        async with aiosqlite.connect(self._path) as db:
            conditions = ["session_id=?"]
            params: list[Any] = [session_id]
            if run_id is not None:
                conditions.append("run_id=?")
                params.append(run_id)
            if after_sequence is not None:
                conditions.append("sequence>?")
                params.append(after_sequence)
            if through_sequence is not None:
                conditions.append("sequence<=?")
                params.append(through_sequence)
            cur = await db.execute(
                f"""
                SELECT
                  id,run_id,sequence,role,parent_id,agent,model_provider,model_id,
                  created_at,completed_at,finish,error_json,tool_call_id,tool_name
                FROM messages
                WHERE {' AND '.join(conditions)}
                ORDER BY sequence ASC
                """,
                tuple(params),
            )
            try:
                msg_rows = await cur.fetchall()
            finally:
                await cur.close()
            out: list[MessageWithParts] = []
            for r in msg_rows:
                info = Message(
                    id=r[0],
                    session_id=session_id,
                    run_id=r[1],
                    sequence=r[2],
                    role=r[3],
                    parent_id=r[4],
                    agent=r[5],
                    model={"provider": r[6], "id": r[7]},
                    created_at=r[8],
                    completed_at=r[9],
                    finish=r[10],
                    error=json.loads(r[11]) if r[11] else None,
                    tool_call_id=r[12],
                    tool_name=r[13],
                )
                parts_cur = await db.execute(
                    "SELECT content_json FROM parts WHERE session_id=? AND message_id=? ORDER BY id ASC",
                    (session_id, info.id),
                )
                try:
                    part_rows = await parts_cur.fetchall()
                finally:
                    await parts_cur.close()
                part_adapter = TypeAdapter(Part)
                parts = [part_adapter.validate_python(json.loads(p[0])) for p in part_rows]
                out.append(MessageWithParts(info=info, parts=parts))
            return out

    async def create_permission_request(self, req: PermissionRequest) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO permission_requests (
                  id,session_id,permission,patterns_json,metadata_json,always_json,tool_json,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    req.id,
                    req.session_id,
                    req.permission,
                    json.dumps(req.patterns),
                    json.dumps(req.metadata),
                    json.dumps(req.always),
                    json.dumps(req.tool) if req.tool else None,
                    "pending",
                    int(time.time() * 1000),
                ),
            )
            await db.commit()

    async def upsert_tool_operation(self, operation: dict[str, Any]) -> None:
        async with aiosqlite.connect(self._path) as db:
            # Parallel subagents can finish model rounds and persist tool
            # operations at the same time. SQLite permits only one writer;
            # wait for the short competing transaction instead of failing the
            # entire child Run with ``database is locked``.
            await db.execute("PRAGMA busy_timeout=30000")
            await db.execute(
                """
                INSERT INTO tool_operations (
                  operation_id,run_id,session_id,message_id,tool_call_id,tool_name,
                  capability,canonical_target,executor_backend,isolation_level,
                  status,error_code,input_json,result_json,created_at,finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(operation_id) DO UPDATE SET
                  capability=excluded.capability,
                  canonical_target=excluded.canonical_target,
                  executor_backend=excluded.executor_backend,
                  isolation_level=excluded.isolation_level,
                  status=excluded.status, error_code=excluded.error_code,
                  result_json=excluded.result_json, finished_at=excluded.finished_at
                """,
                (
                    operation["operation_id"], operation.get("run_id"), operation["session_id"],
                    operation["message_id"], operation["tool_call_id"], operation["tool_name"],
                    operation["capability"], operation["canonical_target"], operation["executor_backend"],
                    operation["isolation_level"], operation["status"], operation.get("error_code"),
                    json.dumps(operation.get("input", {}), ensure_ascii=False),
                    json.dumps(operation.get("result", {}), ensure_ascii=False),
                    operation["created_at"], operation["finished_at"],
                ),
            )
            await db.commit()

    async def list_tool_operations(self, session_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT operation_id,run_id,message_id,tool_call_id,tool_name,
                       capability,canonical_target,executor_backend,isolation_level,
                       status,error_code,input_json,result_json,created_at,finished_at
                FROM tool_operations WHERE session_id=? ORDER BY created_at ASC
                """,
                (session_id,),
            )
            rows = await cur.fetchall()
            return [
                {
                    "operation_id": r[0], "run_id": r[1], "session_id": session_id,
                    "message_id": r[2], "tool_call_id": r[3], "tool_name": r[4],
                    "capability": r[5], "canonical_target": r[6], "executor_backend": r[7],
                    "isolation_level": r[8], "status": r[9], "error_code": r[10],
                    "input": json.loads(r[11]), "result": json.loads(r[12]),
                    "created_at": r[13], "finished_at": r[14],
                }
                for r in rows
            ]

    async def list_run_tool_operations(self, run_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT operation_id,run_id,session_id,message_id,tool_call_id,tool_name,
                       capability,canonical_target,executor_backend,isolation_level,
                       status,error_code,input_json,result_json,created_at,finished_at
                FROM tool_operations WHERE run_id=? ORDER BY created_at ASC
                """,
                (run_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            {
                "operation_id": row[0],
                "run_id": row[1],
                "session_id": row[2],
                "message_id": row[3],
                "tool_call_id": row[4],
                "tool_name": row[5],
                "capability": row[6],
                "canonical_target": row[7],
                "executor_backend": row[8],
                "isolation_level": row[9],
                "status": row[10],
                "error_code": row[11],
                "input": json.loads(row[12]),
                "result": json.loads(row[13]),
                "created_at": row[14],
                "finished_at": row[15],
            }
            for row in rows
        ]

    async def add_artifact(self, artifact: Artifact) -> Artifact:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO artifacts (
                  id, session_id, run_id, message_id, tool_call_id, kind,
                  name, media_type, size_bytes, sha256, storage_path, preview,
                  created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id, artifact.session_id, artifact.run_id,
                    artifact.message_id, artifact.tool_call_id, artifact.kind,
                    artifact.name, artifact.media_type, artifact.size_bytes,
                    artifact.sha256, artifact.storage_path, artifact.preview,
                    artifact.created_at,
                ),
            )
            await db.commit()
        return artifact

    async def get_artifact(self, artifact_id: str) -> Artifact:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        return Artifact(**dict(row))

    async def list_artifacts(
        self, *, run_id: str | None = None, session_id: str | None = None
    ) -> list[Artifact]:
        if run_id is None and session_id is None:
            raise ValueError("run_id or session_id is required")
        field, value = (("run_id", run_id) if run_id is not None else ("session_id", session_id))
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM artifacts WHERE {field}=? ORDER BY created_at ASC",  # noqa: S608
                (value,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [Artifact(**dict(row)) for row in rows]

    async def resolve_permission_request(self, request_id: str, status: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE permission_requests SET status=?, resolved_at=? WHERE id=?",
                (status, int(time.time() * 1000), request_id),
            )
            await db.commit()

    async def add_approval(self, session_id: str, permission: str, pattern: str, action: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO permission_approvals (session_id,permission,pattern,action) VALUES (?,?,?,?)",
                (session_id, permission, pattern, action),
            )
            await db.commit()

    async def list_approvals(self, session_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT permission,pattern,action FROM permission_approvals WHERE session_id=?",
                (session_id,),
            )
            rows = await cur.fetchall()
            return [{"permission": r[0], "pattern": r[1], "action": r[2]} for r in rows]

    async def list_pending_permission_requests(self, session_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id,permission,patterns_json,metadata_json,always_json,tool_json,created_at
                FROM permission_requests
                WHERE session_id=? AND status='pending'
                ORDER BY created_at ASC
                """,
                (session_id,),
            )
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0],
                    "session_id": session_id,
                    "permission": r[1],
                    "patterns": json.loads(r[2]),
                    "metadata": json.loads(r[3]),
                    "always": json.loads(r[4]),
                    "tool": json.loads(r[5]) if r[5] else None,
                    "created_at": r[6],
                }
                for r in rows
            ]

    # ----- Todo operations -----

    async def get_todos(self, session_id: str) -> list[TodoItem]:
        """Get all todos for a session, ordered by position."""
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id, content, status, priority, active_form
                FROM todos
                WHERE session_id = ?
                ORDER BY position ASC
                """,
                (session_id,),
            )
            rows = await cur.fetchall()
            return [
                TodoItem(
                    id=r[0],
                    content=r[1],
                    status=r[2],
                    priority=r[3],
                    activeForm=r[4],
                )
                for r in rows
            ]

    async def update_todos(self, session_id: str, todos: list[TodoItem]) -> None:
        """
        Replace the entire todo list for a session.

        This is an atomic operation:
        1. Delete all existing todos for the session
        2. Insert the new list with proper positions
        """
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            # Delete existing todos
            await db.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))

            # Insert new todos with position
            for position, todo in enumerate(todos):
                await db.execute(
                    """
                    INSERT INTO todos (id, session_id, content, status, priority, active_form, position, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        todo.id,
                        session_id,
                        todo.content,
                        todo.status,
                        todo.priority,
                        todo.activeForm,
                        position,
                        now,
                        now,
                    ),
                )
            await db.commit()

    # ----- Cron job operations -----

    @staticmethod
    def _row_to_cron_job(row: tuple[Any, ...]) -> CronJob:
        return CronJob(
            id=row[0],
            name=row[1],
            session_id=row[2],
            enabled=bool(row[3]),
            schedule=CronSchedule(
                kind=row[4],
                at_ms=row[5],
                every_ms=row[6],
                expr=row[7],
                tz=row[8],
            ),
            payload=CronPayload(kind=row[9], message=row[10]),
            state=CronJobState(
                next_run_at_ms=row[11],
                last_run_at_ms=row[12],
                last_status=row[13],
                last_error=row[14],
                last_assistant_message_id=row[15],
                last_trace_id=row[16],
            ),
            delete_after_run=bool(row[17]),
            created_at_ms=row[18],
            updated_at_ms=row[19],
        )

    async def create_cron_job(self, job: CronJob) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO cron_jobs (
                  id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
                  payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error,
                  last_assistant_message_id, last_trace_id, delete_after_run, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.id,
                    job.name,
                    job.session_id,
                    int(job.enabled),
                    job.schedule.kind,
                    job.schedule.at_ms,
                    job.schedule.every_ms,
                    job.schedule.expr,
                    job.schedule.tz,
                    job.payload.kind,
                    job.payload.message,
                    job.state.next_run_at_ms,
                    job.state.last_run_at_ms,
                    job.state.last_status,
                    job.state.last_error,
                    job.state.last_assistant_message_id,
                    job.state.last_trace_id,
                    int(job.delete_after_run),
                    job.created_at_ms,
                    job.updated_at_ms,
                ),
            )
            await db.commit()

    async def get_cron_job(self, job_id: str) -> CronJob:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT
                  id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
                  payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error,
                  last_assistant_message_id, last_trace_id, delete_after_run, created_at, updated_at
                FROM cron_jobs
                WHERE id=?
                """,
                (job_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise KeyError(f"cron job not found: {job_id}")
            return self._row_to_cron_job(row)

    async def list_cron_jobs(self, *, session_id: str | None = None, include_disabled: bool = True) -> list[CronJob]:
        query = """
            SELECT
              id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
              payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error,
              last_assistant_message_id, last_trace_id, delete_after_run, created_at, updated_at
            FROM cron_jobs
        """
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if not include_disabled:
            clauses.append("enabled=1")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(next_run_at_ms, 9223372036854775807) ASC, created_at ASC"

        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(query, tuple(params))
            rows = await cur.fetchall()
            return [self._row_to_cron_job(row) for row in rows]

    async def list_due_cron_jobs(self, now_ms: int, limit: int = 16) -> list[CronJob]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT
                  id, name, session_id, enabled, schedule_kind, schedule_at_ms, schedule_every_ms, schedule_expr, schedule_tz,
                  payload_kind, payload_message, next_run_at_ms, last_run_at_ms, last_status, last_error,
                  last_assistant_message_id, last_trace_id, delete_after_run, created_at, updated_at
                FROM cron_jobs
                WHERE enabled=1 AND next_run_at_ms IS NOT NULL AND next_run_at_ms<=?
                ORDER BY next_run_at_ms ASC
                LIMIT ?
                """,
                (now_ms, max(1, limit)),
            )
            rows = await cur.fetchall()
            return [self._row_to_cron_job(row) for row in rows]

    async def get_next_cron_wake_ms(self) -> int | None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT MIN(next_run_at_ms) FROM cron_jobs WHERE enabled=1 AND next_run_at_ms IS NOT NULL"
            )
            row = await cur.fetchone()
            if not row or row[0] is None:
                return None
            return int(row[0])

    async def update_cron_job_enabled(self, job_id: str, *, enabled: bool, next_run_at_ms: int | None) -> CronJob:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                UPDATE cron_jobs
                SET enabled=?, next_run_at_ms=?, updated_at=?
                WHERE id=?
                """,
                (int(enabled), next_run_at_ms, now, job_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"cron job not found: {job_id}")
            await db.commit()
        return await self.get_cron_job(job_id)

    async def delete_cron_job(self, job_id: str) -> bool:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
            await db.commit()
            return cur.rowcount > 0

    async def update_cron_job_runtime(
        self,
        job_id: str,
        *,
        next_run_at_ms: int | None,
        last_run_at_ms: int | None,
        last_status: str | None,
        last_error: str | None,
        last_assistant_message_id: str | None,
        last_trace_id: str | None,
        enabled: bool | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            if enabled is None:
                await db.execute(
                    """
                    UPDATE cron_jobs
                    SET next_run_at_ms=?, last_run_at_ms=?, last_status=?, last_error=?, last_assistant_message_id=?, last_trace_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        next_run_at_ms,
                        last_run_at_ms,
                        last_status,
                        last_error,
                        last_assistant_message_id,
                        last_trace_id,
                        now,
                        job_id,
                    ),
                )
            else:
                await db.execute(
                    """
                    UPDATE cron_jobs
                    SET enabled=?, next_run_at_ms=?, last_run_at_ms=?, last_status=?, last_error=?, last_assistant_message_id=?, last_trace_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        int(enabled),
                        next_run_at_ms,
                        last_run_at_ms,
                        last_status,
                        last_error,
                        last_assistant_message_id,
                        last_trace_id,
                        now,
                        job_id,
                    ),
                )
            await db.commit()

    async def create_cron_job_run(self, *, job_id: str, session_id: str, started_at_ms: int) -> int:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                INSERT INTO cron_job_runs (job_id, session_id, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (job_id, session_id, started_at_ms),
            )
            await db.commit()
            run_id = cur.lastrowid
            if run_id is None:
                raise RuntimeError("failed to create cron job run")
            return int(run_id)

    async def finish_cron_job_run(
        self,
        run_id: int,
        *,
        status: str,
        finished_at_ms: int,
        error: str | None = None,
        assistant_message_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE cron_job_runs
                SET status=?, finished_at=?, error=?, assistant_message_id=?, trace_id=?
                WHERE id=?
                """,
                (status, finished_at_ms, error, assistant_message_id, trace_id, run_id),
            )
            await db.commit()

    async def list_cron_job_runs(self, job_id: str, limit: int = 50) -> list[CronJobRun]:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT id, job_id, session_id, started_at, finished_at, status, error, assistant_message_id, trace_id
                FROM cron_job_runs
                WHERE job_id=?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (job_id, max(1, limit)),
            )
            rows = await cur.fetchall()
            return [
                CronJobRun(
                    id=row[0],
                    job_id=row[1],
                    session_id=row[2],
                    started_at_ms=row[3],
                    finished_at_ms=row[4],
                    status=row[5],
                    error=row[6],
                    assistant_message_id=row[7],
                    trace_id=row[8],
                )
                for row in rows
            ]

    # ------------------------------------------------------------------
    # Provider call ledger and circuit state
    # ------------------------------------------------------------------

    @staticmethod
    def _llm_call_columns() -> str:
        return (
            "id,run_id,step_id,parent_call_id,provider,endpoint_hash,model,transport,"
            "capability_profile_version,request_hash,status,semantic_output_started,"
            "fallback_from_call_id,retry_reason,started_at,finished_at,first_event_at,"
            "input_tokens,output_tokens,cached_tokens,reasoning_tokens,"
            "estimated_cost_microusd,pricing_version,error_code,public_error,"
            "provider_request_id,metadata_json"
        )

    @staticmethod
    def _llm_call_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = SQLiteStore._llm_call_columns().split(",")
        result = dict(zip(keys, row, strict=True))
        result["semantic_output_started"] = bool(
            result["semantic_output_started"]
        )
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    @staticmethod
    def _llm_attempt_columns() -> str:
        return (
            "id,call_id,attempt,status,started_at,connected_at,first_event_at,"
            "finished_at,semantic_output_started,http_status,provider_code,"
            "provider_request_id,retry_after_ms,error_code,diagnostic_summary,"
            "input_tokens,output_tokens,cached_tokens,reasoning_tokens"
        )

    @staticmethod
    def _llm_attempt_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = SQLiteStore._llm_attempt_columns().split(",")
        result = dict(zip(keys, row, strict=True))
        result["semantic_output_started"] = bool(
            result["semantic_output_started"]
        )
        return result

    async def create_llm_call(
        self,
        *,
        call_id: str,
        run_id: str,
        step_id: str,
        provider: str,
        endpoint_hash: str,
        model: str,
        transport: str,
        capability_profile_version: str,
        request_hash: str,
        parent_call_id: str | None = None,
        fallback_from_call_id: str | None = None,
        retry_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO llm_calls (
                  id,run_id,step_id,parent_call_id,provider,endpoint_hash,model,
                  transport,capability_profile_version,request_hash,status,
                  fallback_from_call_id,retry_reason,started_at,metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'planned',?,?,?,?)
                """,
                (
                    call_id,
                    run_id,
                    step_id,
                    parent_call_id,
                    provider,
                    endpoint_hash,
                    model,
                    transport,
                    capability_profile_version,
                    request_hash,
                    fallback_from_call_id,
                    retry_reason,
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            await db.commit()
        return await self.get_llm_call(call_id)

    async def start_llm_attempt(
        self,
        *,
        attempt_id: str,
        call_id: str,
        attempt: int,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO llm_call_attempts (
                      id,call_id,attempt,status,started_at
                    ) VALUES (?,?,?,'running',?)
                    """,
                    (attempt_id, call_id, attempt, now),
                )
                await db.execute(
                    "UPDATE llm_calls SET status='running' WHERE id=? AND status IN ('planned','retrying')",
                    (call_id,),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        attempts = await self.list_llm_call_attempts(call_id)
        return next(item for item in attempts if item["id"] == attempt_id)

    async def mark_llm_attempt_connected(
        self,
        *,
        call_id: str,
        attempt_id: str,
        provider_request_id: str | None,
        now_ms: int | None = None,
    ) -> None:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE llm_call_attempts
                SET connected_at=COALESCE(connected_at,?),
                    provider_request_id=COALESCE(provider_request_id,?)
                WHERE id=? AND status='running'
                """,
                (now, provider_request_id, attempt_id),
            )
            await db.execute(
                "UPDATE llm_calls SET provider_request_id=COALESCE(provider_request_id,?) WHERE id=?",
                (provider_request_id, call_id),
            )
            await db.commit()

    async def mark_llm_semantic_output(
        self,
        *,
        call_id: str,
        attempt_id: str,
        now_ms: int | None = None,
    ) -> None:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE llm_call_attempts
                SET semantic_output_started=1, first_event_at=COALESCE(first_event_at,?)
                WHERE id=? AND status='running'
                """,
                (now, attempt_id),
            )
            await db.execute(
                """
                UPDATE llm_calls
                SET semantic_output_started=1, first_event_at=COALESCE(first_event_at,?)
                WHERE id=?
                """,
                (now, call_id),
            )
            await db.commit()

    async def finish_llm_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        error_code: str | None = None,
        diagnostic_summary: str | None = None,
        http_status: int | None = None,
        provider_code: str | None = None,
        provider_request_id: str | None = None,
        retry_after_ms: int | None = None,
        usage: dict[str, int | None] | None = None,
        now_ms: int | None = None,
    ) -> None:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        usage = usage or {}
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE llm_call_attempts
                SET status=?,finished_at=?,http_status=?,provider_code=?,
                    provider_request_id=COALESCE(?,provider_request_id),
                    retry_after_ms=?,error_code=?,diagnostic_summary=?,
                    input_tokens=?,output_tokens=?,cached_tokens=?,reasoning_tokens=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    now,
                    http_status,
                    provider_code,
                    provider_request_id,
                    retry_after_ms,
                    error_code,
                    diagnostic_summary,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("cached_tokens"),
                    usage.get("reasoning_tokens"),
                    attempt_id,
                ),
            )
            await db.commit()

    async def set_llm_call_retrying(self, call_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE llm_calls SET status='retrying' WHERE id=? AND status='running'",
                (call_id,),
            )
            await db.commit()

    async def finish_llm_call(
        self,
        call_id: str,
        *,
        status: str,
        error_code: str | None = None,
        public_error: str | None = None,
        provider_request_id: str | None = None,
        usage: dict[str, int | None] | None = None,
        estimated_cost_microusd: int | None = None,
        pricing_version: str | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        usage = usage or {}
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE llm_calls
                SET status=?,finished_at=?,input_tokens=?,output_tokens=?,
                    cached_tokens=?,reasoning_tokens=?,estimated_cost_microusd=?,
                    pricing_version=?,error_code=?,public_error=?,
                    provider_request_id=COALESCE(?,provider_request_id)
                WHERE id=? AND status IN ('planned','running','retrying')
                """,
                (
                    status,
                    now,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("cached_tokens"),
                    usage.get("reasoning_tokens"),
                    estimated_cost_microusd,
                    pricing_version,
                    error_code,
                    public_error,
                    provider_request_id,
                    call_id,
                ),
            )
            await db.commit()
        return await self.get_llm_call(call_id)

    async def get_llm_call(self, call_id: str) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"SELECT {self._llm_call_columns()} FROM llm_calls WHERE id=?",
                (call_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            raise KeyError(f"llm call not found: {call_id}")
        return self._llm_call_from_row(row)

    async def list_run_llm_calls(self, run_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"SELECT {self._llm_call_columns()} FROM llm_calls WHERE run_id=? ORDER BY started_at,id",
                (run_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._llm_call_from_row(row) for row in rows]

    async def list_llm_call_attempts(self, call_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                f"SELECT {self._llm_attempt_columns()} FROM llm_call_attempts WHERE call_id=? ORDER BY attempt",
                (call_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._llm_attempt_from_row(row) for row in rows]

    async def get_run_llm_totals(self, run_id: str) -> dict[str, int]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens),0),
                       COALESCE(SUM(output_tokens),0),
                       COALESCE(SUM(estimated_cost_microusd),0)
                FROM llm_calls WHERE run_id=?
                """,
                (run_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            attempt_cursor = await db.execute(
                """
                SELECT COUNT(*) FROM llm_call_attempts
                WHERE call_id IN (SELECT id FROM llm_calls WHERE run_id=?)
                """,
                (run_id,),
            )
            attempt_row = await attempt_cursor.fetchone()
            await attempt_cursor.close()
        return {
            "calls": int(row[0] if row else 0),
            "attempts": int(attempt_row[0] if attempt_row else 0),
            "input_tokens": int(row[1] if row else 0),
            "output_tokens": int(row[2] if row else 0),
            "cost_microusd": int(row[3] if row else 0),
        }

    async def acquire_provider_circuit(
        self,
        *,
        circuit_key: str,
        owner_id: str,
        now_ms: int,
    ) -> bool:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT state,retry_at,half_open_owner FROM provider_circuits WHERE circuit_key=?",
                    (circuit_key,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if not row:
                    await db.execute(
                        """
                        INSERT INTO provider_circuits (
                          circuit_key,state,failure_count,revision,updated_at
                        ) VALUES (?,'closed',0,0,?)
                        """,
                        (circuit_key, now_ms),
                    )
                    await db.commit()
                    return True
                state, retry_at, half_open_owner = row
                if state == "closed":
                    await db.commit()
                    return True
                if state == "open" and retry_at is not None and int(retry_at) <= now_ms:
                    await db.execute(
                        """
                        UPDATE provider_circuits
                        SET state='half_open',half_open_owner=?,revision=revision+1,updated_at=?
                        WHERE circuit_key=? AND state='open'
                        """,
                        (owner_id, now_ms, circuit_key),
                    )
                    await db.commit()
                    return True
                allowed = state == "half_open" and half_open_owner == owner_id
                await db.commit()
                return allowed
            except BaseException:
                await db.rollback()
                raise

    async def record_provider_circuit_success(
        self, *, circuit_key: str, now_ms: int
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE provider_circuits
                SET state='closed',failure_count=0,window_started_at=NULL,
                    opened_at=NULL,retry_at=NULL,half_open_owner=NULL,
                    revision=revision+1,updated_at=?
                WHERE circuit_key=?
                """,
                (now_ms, circuit_key),
            )
            await db.commit()

    async def record_provider_circuit_failure(
        self,
        *,
        circuit_key: str,
        threshold: int,
        window_ms: int,
        cooldown_ms: int,
        now_ms: int,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT state,failure_count,window_started_at
                    FROM provider_circuits WHERE circuit_key=?
                    """,
                    (circuit_key,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                state = str(row[0]) if row else "closed"
                count = int(row[1]) if row else 0
                window_started = int(row[2]) if row and row[2] is not None else now_ms
                if now_ms - window_started > window_ms:
                    count = 0
                    window_started = now_ms
                count += 1
                should_open = state == "half_open" or count >= threshold
                next_state = "open" if should_open else "closed"
                opened_at = now_ms if should_open else None
                retry_at = now_ms + cooldown_ms if should_open else None
                await db.execute(
                    """
                    INSERT INTO provider_circuits (
                      circuit_key,state,failure_count,window_started_at,opened_at,
                      retry_at,half_open_owner,revision,updated_at
                    ) VALUES (?,?,?,?,?,?,NULL,0,?)
                    ON CONFLICT(circuit_key) DO UPDATE SET
                      state=excluded.state,failure_count=excluded.failure_count,
                      window_started_at=excluded.window_started_at,
                      opened_at=excluded.opened_at,retry_at=excluded.retry_at,
                      half_open_owner=NULL,revision=provider_circuits.revision+1,
                      updated_at=excluded.updated_at
                    """,
                    (
                        circuit_key,
                        next_state,
                        count,
                        window_started,
                        opened_at,
                        retry_at,
                        now_ms,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.get_provider_circuit(circuit_key)

    async def get_provider_circuit(self, circuit_key: str) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT circuit_key,state,failure_count,window_started_at,opened_at,
                       retry_at,half_open_owner,revision,updated_at
                FROM provider_circuits WHERE circuit_key=?
                """,
                (circuit_key,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return {
                "circuit_key": circuit_key,
                "state": "closed",
                "failure_count": 0,
                "revision": 0,
            }
        keys = (
            "circuit_key",
            "state",
            "failure_count",
            "window_started_at",
            "opened_at",
            "retry_at",
            "half_open_owner",
            "revision",
            "updated_at",
        )
        return dict(zip(keys, row, strict=True))

    async def list_provider_circuits(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT circuit_key,state,failure_count,window_started_at,opened_at,
                       retry_at,half_open_owner,revision,updated_at
                FROM provider_circuits ORDER BY circuit_key
                """
            )
            rows = await cursor.fetchall()
            await cursor.close()
        keys = (
            "circuit_key",
            "state",
            "failure_count",
            "window_started_at",
            "opened_at",
            "retry_at",
            "half_open_owner",
            "revision",
            "updated_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]
