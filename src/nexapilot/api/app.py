from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from nexapilot.agents import load_agent_registry
from nexapilot.agents.service import AgentService
from nexapilot.agents.types import RunRequest
from nexapilot.agents.workspaces import AgentWorkspaceDirty, AgentWorkspaceError
from nexapilot.artifacts import ArtifactIntegrityError, ArtifactStore
from nexapilot.bus.bus import Bus, Event
from nexapilot.channels import ChannelBus, ChannelManager, ChannelSessionBridge
from nexapilot.channels.events import InboundChannelMessage, OutboundChannelMessage
from nexapilot.config import (
    GLOBAL_CONFIG_PATHS,
    _deep_merge,
    _load_yaml_file,
    config_to_dict,
    generate_default_yaml,
    get_config_sources,
    load,
    mask_sensitive,
    project_config_paths,
    redact_sensitive,
    restore_redacted_sensitive,
    save_config,
)
from nexapilot.config_schema import CONFIG_FIELD_META
from nexapilot.cron import CronJobExecResult, CronService
from nexapilot.evaluation.feedback import (
    EvalCandidateReview,
    FeedbackService,
    FeedbackSubmission,
    redact_feedback_text,
)
from nexapilot.hookdefs import Hook
from nexapilot.hooks import Hooker, loghook
from nexapilot.llm.capabilities import CapabilityResolver
from nexapilot.llm.gateway import ProviderGateway
from nexapilot.llm.openai_chat import OpenAIChatProvider
from nexapilot.llm.openai_responses import OpenAIResponsesProvider
from nexapilot.llm.routing import ModelRouter
from nexapilot.log import init_logging, logger
from nexapilot.loop.interrupt import InterruptManager
from nexapilot.loop.session_loop import SessionLoop
from nexapilot.mcp import MCPManager, MCPServerConfig, MCPToolAdapter, load_mcp_configs
from nexapilot.memory import CoreMemoryBuilder, MemoryService, create_memory_hooks
from nexapilot.model import (
    AddMessageRequest,
    AgentWorkspaceReleaseRequest,
    CreateCronJobRequest,
    CreateGoalRequest,
    CreatePlanRequest,
    CreateProjectRequest,
    CreateSessionRequest,
    CronJob,
    CronJobEnabledRequest,
    CronJobRunRequest,
    DaytonaRuntimeConfig,
    Message,
    ModelRef,
    PermissionReply,
    PermissionRule,
    Project,
    RenameSessionRequest,
    RenameProjectRequest,
    ReorderPlanRequest,
    RevisionActionRequest,
    Session,
    SessionRuntime,
    TaskReleaseRequest,
    TaskTakeoverRequest,
    TaskTransitionRequest,
    TextPart,
)
from nexapilot.observability.langfuse_client import (
    flush_langfuse,
    init_langfuse,
    shutdown_langfuse,
)
from nexapilot.observability.langfuse_hook import create_langfuse_hook
from nexapilot.permission.service import PermissionService
from nexapilot.run_lifecycle import DurableRunReconciler
from nexapilot.run_workspace import RunWorkspaceService
from nexapilot.runtime import (
    DaytonaManager,
    DaytonaOperationError,
    DaytonaUnavailableError,
)
from nexapilot.security import evaluate_request_security, security_headers
from nexapilot.skills.loader import SkillLoader
from nexapilot.store.sqlite import SQLiteStore
from nexapilot.task_runtime import (
    TaskRuntimeConflict,
    TaskRuntimeNotFound,
    TaskRuntimeService,
    TaskRuntimeValidationError,
)
from nexapilot.tools.bash import BashCtx, BashTool
from nexapilot.tools.daytona import (
    DaytonaBashTool,
    DaytonaCtx,
    DaytonaGlobTool,
    DaytonaGrepTool,
    DaytonaReadTool,
    DaytonaWriteTool,
)
from nexapilot.tools.files import FileCtx, ReadTool, WriteTool
from nexapilot.tools.grep import GlobTool, GrepTool, SearchCtx
from nexapilot.tools.kb_search import KBSearchCtx, KBSearchTool
from nexapilot.tools.memory import MemoryGetTool, MemorySearchTool, MemoryToolCtx
from nexapilot.tools.registry import ToolRegistry
from nexapilot.tools.skill import SkillCtx, SkillTool
from nexapilot.tools.task import TaskTool
from nexapilot.tools.todo import TodoWriteTool
from nexapilot.tools.web import TavilySearchTool, WebFetchTool, WebSearchCtx

cfg = load()
init_logging(
    level=cfg.logging.level,
    console=cfg.logging.console,
    file=cfg.logging.file,
    log_dir=cfg.logging.dir,
    rotation=cfg.logging.rotation,
    retention=cfg.logging.retention,
)
logger.info(
    "NexaPilot starting",
    event="app.start",
    model=cfg.openai.model,
    openai_base_url=cfg.openai.base_url,
    openai_transport=cfg.openai.transport,
    reasoning_effort=cfg.openai.reasoning_effort,
)
bus = Bus()
store = SQLiteStore(cfg.db_path)
artifact_store = ArtifactStore(
    store,
    Path(cfg.db_path).resolve().parent / "artifacts",
)
hooks = Hooker()
lh = loghook(cfg=cfg)
if lh:
    hooks.add(lh)

# Initialize Langfuse tracing
init_langfuse(cfg.langfuse)

# Add Langfuse hook
langfuse_hook = create_langfuse_hook()
if langfuse_hook:
    hooks.add(langfuse_hook)

perm = PermissionService(bus, store, hooks)
mcp_manager = MCPManager(bus=bus)
daytona_manager = DaytonaManager(cfg.daytona, store)
memory_service = MemoryService(cfg=cfg, store=store)
task_runtime = TaskRuntimeService(cfg.db_path, bus)
run_workspace = RunWorkspaceService(store)
feedback_service = FeedbackService(store)
hooks.add(create_memory_hooks(cfg=cfg, store=store, service=memory_service))

# Initialize KB backend (optional — disabled when base_url is empty)
kb_client = None
vlm_client = None

if cfg.kb.base_url:
    from nexapilot.kb.lightrag_client import LightRAGClient

    kb_client = LightRAGClient(base_url=cfg.kb.base_url, api_key=cfg.kb.api_key)
    logger.info(
        "KB backend enabled",
        event="kb.init",
        backend=cfg.kb.backend,
        base_url=cfg.kb.base_url,
    )

if cfg.vlm.backend == "paddleocr" and cfg.vlm.api_url:
    from nexapilot.kb.paddleocr_client import PaddleOCRClient

    vlm_client = PaddleOCRClient(
        api_url=cfg.vlm.api_url,
        api_key=cfg.vlm.api_key,
        poll_interval=cfg.vlm.poll_interval,
        timeout=cfg.vlm.timeout,
    )
    logger.info("VLM parser enabled", event="vlm.init", backend=cfg.vlm.backend)

chat_llm = OpenAIChatProvider(
    base_url=cfg.openai.base_url,
    api_key=cfg.openai.api_key,
    model=cfg.openai.model,
    langfuse_enabled=cfg.langfuse.enabled,
)
responses_llm = OpenAIResponsesProvider(
    base_url=cfg.openai.base_url,
    api_key=cfg.openai.api_key,
    model=cfg.openai.model,
    reasoning_effort=cfg.openai.reasoning_effort,
)
gateway_adapters = {
    "chat_completions": chat_llm,
    "responses": responses_llm,
}
if cfg.model_gateway.enabled:
    for provider_id, provider in cfg.model_gateway.providers.items():
        provider_api_key = str(os.getenv(provider.api_key_env, "")).strip()
        if not provider_api_key:
            raise RuntimeError(
                f"model gateway provider {provider_id!r} requires environment variable "
                f"{provider.api_key_env!r}"
            )
        if "chat_completions" in provider.transports:
            gateway_adapters[f"{provider_id}:chat_completions"] = OpenAIChatProvider(
                base_url=provider.base_url,
                api_key=provider_api_key,
                model=cfg.openai.model,
                langfuse_enabled=cfg.langfuse.enabled,
            )
        if "responses" in provider.transports:
            gateway_adapters[f"{provider_id}:responses"] = OpenAIResponsesProvider(
                base_url=provider.base_url,
                api_key=provider_api_key,
                model=cfg.openai.model,
                reasoning_effort=cfg.openai.reasoning_effort,
            )
if cfg.openai.resilience.enabled or cfg.model_gateway.enabled:
    llm = ProviderGateway(
        config=cfg.openai,
        store=store,
        adapters=gateway_adapters,
        bus=bus,
        router=ModelRouter(cfg.model_gateway, cfg.openai),
    )
elif cfg.openai.transport == "responses":
    llm = responses_llm
else:
    llm = chat_llm
interrupt = InterruptManager()
channel_bus = ChannelBus()
channel_manager = ChannelManager(cfg, channel_bus)
channel_bridge: ChannelSessionBridge | None = None
cron_service: CronService | None = None
effective_agent_registry = load_agent_registry(cfg.default_worktree)
agent_service = AgentService(
    cfg=cfg,
    bus=bus,
    store=store,
    perm=perm,
    llm=llm,
    interrupt=interrupt,
    hooks=hooks,
    memory_service=memory_service,
    daytona_manager=daytona_manager,
    mcp_manager=mcp_manager,
    task_runtime=task_runtime,
    kb_client=kb_client,
    registry=effective_agent_registry,
    artifact_store=artifact_store,
)
run_reconciler = DurableRunReconciler(
    store=store,
    bus=bus,
    owner_id=agent_service.owner_id,
    lease_duration_ms=cfg.durable_run.lease_duration_ms,
    interval_ms=cfg.durable_run.heartbeat_interval_ms,
)
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOG_FILE_RE = re.compile(r"^nexapilot_(\d{4}-\d{2}-\d{2})\.jsonl$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _default_rules() -> list[PermissionRule]:
    return [
        PermissionRule(
            permission="*", pattern="*", action=cfg.default_permission_action
        ),
    ]


def _build_feishu_channel_rules(runtime_cfg: Any) -> list[PermissionRule]:
    feishu_cfg = runtime_cfg.channels.feishu
    mode = str(getattr(feishu_cfg, "permission_mode", "deny") or "deny").strip().lower()
    allowed_cmds = [
        str(x).strip()
        for x in (getattr(feishu_cfg, "allowed_bash_commands", []) or [])
        if str(x).strip()
    ]

    if mode == "allow":
        return [PermissionRule(permission="*", pattern="*", action="allow")]

    if mode == "commands":
        rules: list[PermissionRule] = [
            PermissionRule(permission="bash", pattern=cmd, action="allow")
            for cmd in allowed_cmds
        ]
        rules.append(PermissionRule(permission="*", pattern="*", action="deny"))
        return rules

    return [PermissionRule(permission="*", pattern="*", action="deny")]


def _same_rules(a: list[PermissionRule], b: list[PermissionRule]) -> bool:
    return [r.model_dump() for r in a] == [r.model_dump() for r in b]


async def _get_session_or_404(session_id: str) -> Session:
    try:
        return await store.get_session(session_id)
    except KeyError as e:
        detail = str(e.args[0]) if e.args else "session not found"
        raise HTTPException(status_code=404, detail=detail)


async def _refresh_daytona_runtime_metadata(session: Session) -> Session:
    if session.runtime.backend != "daytona":
        return session
    daytona_cfg = session.runtime.daytona
    if not daytona_cfg or not daytona_cfg.sandbox_id:
        return session
    if daytona_cfg.sandbox_name:
        return session
    try:
        return await daytona_manager.ensure_session_runtime_async(session)
    except Exception as exc:
        logger.warning(
            "Failed to refresh Daytona session runtime metadata",
            event="daytona.session.metadata.refresh_failed",
            session_id=session.id,
            error=str(exc),
        )
        return session


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
        return lock


async def _add_user_message(session: Session, text: str, *, source: str = "api") -> str:
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    return await agent_service.add_user_message(session, text, source=source)


async def _build_tools(session: Session) -> ToolRegistry:
    return await agent_service.build_tools(session, session.agent_name)


async def _run_agent_once(
    session: Session, *, source: str = "api"
) -> tuple[str, str | None]:
    lock = await _get_session_lock(session.id)
    async with lock:
        logger.info(
            "Running session loop",
            event="session.run.requested",
            session_id=session.id,
            source=source,
        )
        result = await agent_service.run(
            RunRequest(
                session_id=session.id,
                agent_name=session.agent_name,
                source=source,
                root_session_id=session.root_session_id or session.id,
                parent_session_id=session.parent_session_id,
                parent_tool_call_id=session.parent_tool_call_id,
                limits=agent_service.get_agent(session.agent_name).limits,
            )
        )
        return result.assistant_message_id, result.trace_id


def _require_cron_service() -> CronService:
    if not cron_service:
        raise HTTPException(status_code=503, detail="cron service not ready")
    return cron_service


async def _execute_cron_job(job: CronJob) -> CronJobExecResult | None:
    session = await store.get_session(job.session_id)
    await _add_user_message(session, job.payload.message, source=f"cron:{job.id}")
    assistant_message_id, trace_id = await _run_agent_once(
        session, source=f"cron:{job.id}"
    )
    return CronJobExecResult(
        assistant_message_id=assistant_message_id, trace_id=trace_id
    )


async def _extract_assistant_text(session_id: str, message_id: str) -> str:
    history = await store.list_messages(session_id)
    target = next((m for m in history if m.info.id == message_id), None)
    if not target:
        return ""
    chunks: list[str] = []
    for part in target.parts:
        if getattr(part, "type", None) == "text":
            text = getattr(part, "text", "")
            if text:
                chunks.append(text)
    return "".join(chunks).strip()


async def _resolve_or_create_channel_session(msg: InboundChannelMessage) -> Session:
    runtime_cfg = _load_effective_config()
    bound_session_id = await store.get_channel_session(msg.channel, msg.chat_id)
    if bound_session_id:
        try:
            session = await store.get_session(bound_session_id)
            desired_rules = _build_feishu_channel_rules(runtime_cfg)
            if not _same_rules(session.permission_rules, desired_rules):
                session = await store.update_session_permission_rules(
                    bound_session_id, desired_rules
                )
                logger.info(
                    "Channel session permission rules synchronized",
                    event="channel.session.rules.adjusted",
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_id=bound_session_id,
                    permission_mode=runtime_cfg.channels.feishu.permission_mode,
                )
            return session
        except KeyError:
            logger.warning(
                "Channel session binding points to missing session; recreating",
                event="channel.session.binding_stale",
                channel=msg.channel,
                chat_id=msg.chat_id,
                session_id=bound_session_id,
            )

    now = int(time.time() * 1000)
    title = f"[{msg.channel}] {msg.chat_id}"
    session_id = str(uuid4())
    s = Session(
        id=session_id,
        title=title,
        worktree=runtime_cfg.default_worktree,
        cwd=runtime_cfg.default_worktree,
        created_at=now,
        updated_at=now,
        permission_rules=_build_feishu_channel_rules(runtime_cfg),
        runtime=SessionRuntime(backend="local"),
        kind="primary",
        agent_name="primary",
        root_session_id=session_id,
    )
    await store.create_session(s)
    await memory_service.ensure_worktree(s.worktree)
    await store.bind_channel_session(
        channel=msg.channel,
        chat_id=msg.chat_id,
        session_id=s.id,
        sender_id=msg.sender_id,
    )
    await bus.publish(
        Event(
            type="session.created",
            properties={"session_id": s.id, "info": s.model_dump()},
        )
    )
    logger.info(
        "Channel session created",
        event="channel.session.created",
        channel=msg.channel,
        chat_id=msg.chat_id,
        sender_id=msg.sender_id,
        session_id=s.id,
    )
    return s


async def _process_channel_inbound(
    msg: InboundChannelMessage,
) -> OutboundChannelMessage | None:
    session = await _resolve_or_create_channel_session(msg)
    text = msg.content.strip()
    if not text:
        return None

    await _add_user_message(session, text, source=f"channel:{msg.channel}")
    assistant_message_id, trace_id = await _run_agent_once(
        session, source=f"channel:{msg.channel}"
    )
    content = await _extract_assistant_text(session.id, assistant_message_id)
    if not content:
        history = await store.list_messages(session.id)
        assistant = next(
            (m.info for m in history if m.info.id == assistant_message_id), None
        )
        if assistant and assistant.finish == "blocked":
            content = "工具调用被权限策略拦截。请在 Channel Config 里调整 permission_mode（allow / commands）并配置可执行命令后重试。"
        else:
            content = "(empty response)"

    metadata: dict[str, Any] = {
        "session_id": session.id,
        "assistant_message_id": assistant_message_id,
        "source": f"channel:{msg.channel}",
    }
    if trace_id:
        metadata["trace_id"] = trace_id
        metadata["trace_url"] = f"{cfg.langfuse.base_url}/trace/{trace_id}"

    return OutboundChannelMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=content,
        metadata=metadata,
    )


def _load_effective_config() -> Any:
    """Load latest merged config from disk (global + project)."""
    return load(cfg.default_worktree)


def _parse_allow_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        text = value.replace("\r", "\n")
        items: list[str] = []
        for line in text.split("\n"):
            for token in line.split(","):
                s = token.strip()
                if s:
                    items.append(s)
        return items
    return []


def _parse_line_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        text = value.replace("\r", "\n")
        items: list[str] = []
        for line in text.split("\n"):
            for token in line.split(","):
                s = token.strip()
                if s:
                    items.append(s)
        return items
    return []


def _serialize_feishu_config(channel_cfg: Any) -> dict[str, Any]:
    return {
        "enabled": bool(channel_cfg.enabled),
        "app_id": channel_cfg.app_id or "",
        "app_secret_set": bool(channel_cfg.app_secret),
        "encrypt_key_set": bool(channel_cfg.encrypt_key),
        "verification_token_set": bool(channel_cfg.verification_token),
        "allow_from": list(channel_cfg.allow_from or []),
        "permission_mode": str(
            getattr(channel_cfg, "permission_mode", "deny") or "deny"
        ),
        "allowed_bash_commands": list(
            getattr(channel_cfg, "allowed_bash_commands", []) or []
        ),
    }


async def _reload_channels_runtime(runtime_cfg: Any) -> None:
    global channel_manager
    old_manager = channel_manager
    await old_manager.stop_all()
    channel_manager = ChannelManager(runtime_cfg, channel_bus)
    await channel_manager.start_all()


app = FastAPI(title="NexaPilot", version="0.1.0")


@app.exception_handler(TaskRuntimeNotFound)
async def _task_runtime_not_found(_request: Request, exc: TaskRuntimeNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(TaskRuntimeConflict)
async def _task_runtime_conflict(_request: Request, exc: TaskRuntimeConflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(TaskRuntimeValidationError)
async def _task_runtime_validation(_request: Request, exc: TaskRuntimeValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})

_web_dir = Path(__file__).resolve().parent.parent / "web"
app.mount(
    "/static/styles",
    StaticFiles(directory=str(_web_dir / "styles")),
    name="static-styles",
)
app.mount(
    "/static/vendor",
    StaticFiles(directory=str(_web_dir / "vendor")),
    name="static-vendor",
)


@app.middleware("http")
async def local_console_security(request: Request, call_next):
    client_host = request.client.host if request.client else None
    decision = evaluate_request_security(
        method=request.method,
        scheme=request.url.scheme,
        host_header=request.headers.get("host", ""),
        client_host=client_host,
        origin=request.headers.get("origin"),
        sec_fetch_site=request.headers.get("sec-fetch-site"),
    )
    if not decision.allowed:
        logger.warning(
            "Rejected request outside the local console security boundary",
            event="security.request.denied",
            error_code=decision.error_code,
            method=request.method,
            path=request.url.path,
            client_host=client_host,
        )
        response = JSONResponse(
            status_code=decision.status_code,
            content={
                "error": {"code": decision.error_code, "message": decision.detail}
            },
        )
    else:
        response = await call_next(request)

    for name, value in security_headers().items():
        response.headers[name] = value
    if request.url.path.startswith("/config") or request.url.path.startswith(
        "/channels/config"
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.on_event("startup")
async def _startup():
    global channel_bridge, cron_service
    await store.init()
    reconciled_workspaces = await agent_service.workspace_manager.reconcile()
    if reconciled_workspaces:
        logger.warning(
            "Agent workspaces reconciled",
            event="agent.workspace.reconciled",
            count=len(reconciled_workspaces),
        )
    await task_runtime.start()
    if cfg.durable_run.enabled:
        recovered = await run_reconciler.start()
        logger.info(
            "Durable Run reconciliation ready",
            event="run.reconciler.ready",
            recovered_runs=len(recovered),
            owner_id=agent_service.owner_id,
        )
    await memory_service.start()
    await hooks.trigger(Hook.Config, {"config": cfg}, {})

    # Initialize MCP servers
    mcp_configs = load_mcp_configs(cfg.default_worktree)
    if mcp_configs:
        await mcp_manager.initialize(mcp_configs)

    async def forward():
        async for e in bus.subscribe("*"):
            await hooks.trigger(Hook.Event, {"event": e}, {})

    asyncio.create_task(forward())

    # Start channel adapters and channel->session bridge
    await channel_manager.start_all()
    channel_bridge = ChannelSessionBridge(
        bus=channel_bus, process_inbound=_process_channel_inbound
    )
    await channel_bridge.start()

    cron_service = CronService(store=store, on_job=_execute_cron_job)
    await cron_service.start()


@app.on_event("shutdown")
async def _shutdown():
    """Shutdown handler to flush Langfuse events and close MCP connections."""
    global cron_service
    if cron_service:
        await cron_service.stop()
        cron_service = None
    await run_reconciler.stop()
    await task_runtime.stop()
    await memory_service.stop()
    if channel_bridge:
        await channel_bridge.stop()
    await channel_manager.stop_all()
    await mcp_manager.shutdown()
    shutdown_langfuse()


@app.get("/")
async def home():
    nonce = secrets.token_urlsafe(24)
    document = (_web_dir / "index.html").read_text(encoding="utf-8")
    document = document.replace("__CSP_NONCE__", nonce)
    content_security_policy = "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )
    return HTMLResponse(
        document,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": content_security_policy,
        },
    )


@app.get("/config")
async def get_config():
    return mask_sensitive(config_to_dict(cfg))


@app.get("/config/schema")
async def get_config_schema():
    """Return field metadata for the Settings UI."""
    return {
        key: {
            "key": meta.key,
            "description": meta.description,
            "default": meta.default,
            "sensitive": meta.sensitive,
            "choices": meta.choices,
            "type": meta.type_name,
        }
        for key, meta in CONFIG_FIELD_META.items()
    }


@app.get("/config/sources")
async def get_config_sources_endpoint():
    return {"sources": get_config_sources(cfg.default_worktree)}


def _resolve_memory_workspace(workspace: str) -> str:
    value = workspace.strip()
    if not value:
        raise HTTPException(status_code=400, detail="workspace is required")
    return str(Path(value).expanduser().resolve())


@app.get("/memory/status")
async def get_memory_status():
    processing = await store.get_memory_processing_status()
    return {
        "features": {
            "memory": cfg.memory.enabled,
            "processing": cfg.memory.processing.enabled,
            "episodic": cfg.memory.episodic.enabled,
            "semantic": cfg.memory.semantic.enabled,
            "core": cfg.memory.core.enabled,
            "context_manager": cfg.memory.context_manager.enabled,
            "shadow_mode": cfg.memory.context_manager.shadow_mode,
        },
        **processing,
    }


@app.get("/memory/outbox")
async def get_memory_outbox(status: str | None = None, limit: int = 100):
    events = await store.list_outbox_events(
        status=status, limit=min(max(limit, 1), 500)
    )
    return {"events": [event.model_dump() for event in events]}


@app.post("/memory/process")
async def process_memory_once():
    return {"claimed": await memory_service.process_pending_once()}


@app.get("/memory/episodes")
async def get_memory_episodes(workspace: str, limit: int = 50, offset: int = 0):
    resolved = _resolve_memory_workspace(workspace)
    episodes = await store.list_episodes(
        resolved, limit=min(max(limit, 1), 500), offset=max(offset, 0)
    )
    return {"episodes": [episode.model_dump() for episode in episodes]}


@app.delete("/memory/episodes/{episode_id}")
async def delete_memory_episode(episode_id: str):
    try:
        await store.get_episode(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await store.delete_episode(episode_id)
    return {"ok": True, "episode_id": episode_id}


@app.get("/memory/semantic")
async def get_semantic_memories(
    workspace: str,
    namespace: str = "project",
    status: str | None = "active",
    limit: int = 100,
    offset: int = 0,
):
    resolved = _resolve_memory_workspace(workspace)
    memories = await store.list_semantic_memories(
        resolved,
        namespace=namespace,
        status=status,
        limit=min(max(limit, 1), 500),
        offset=max(offset, 0),
    )
    return {"memories": [memory.model_dump() for memory in memories]}


@app.delete("/memory/semantic/{memory_id}")
async def forget_semantic_memory(memory_id: str):
    try:
        memory = await store.forget_semantic_memory(memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    blocks = await CoreMemoryBuilder(
        store=store,
        max_tokens=cfg.memory.core.max_tokens,
    ).rebuild(
        memory.workspace, namespace=memory.namespace, now_ms=int(time.time() * 1000)
    )
    return {
        "memory": memory.model_dump(),
        "core_blocks_rebuilt": len(blocks),
    }


@app.post("/memory/semantic/{memory_id}/activate")
async def activate_semantic_memory_candidate(memory_id: str):
    try:
        action, memory = await store.promote_semantic_memory(memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    blocks = await CoreMemoryBuilder(
        store=store,
        max_tokens=cfg.memory.core.max_tokens,
    ).rebuild(
        memory.workspace, namespace=memory.namespace, now_ms=int(time.time() * 1000)
    )
    return {
        "action": action,
        "memory": memory.model_dump(),
        "core_blocks_rebuilt": len(blocks),
    }


@app.get("/memory/core")
async def get_core_memory(workspace: str, namespace: str = "project"):
    resolved = _resolve_memory_workspace(workspace)
    blocks = await store.list_core_memory_blocks(resolved, namespace=namespace)
    return {
        "blocks": [block.model_dump() for block in blocks],
        "rendered": CoreMemoryBuilder.render(blocks),
    }


@app.post("/memory/core/rebuild")
async def rebuild_core_memory(workspace: str, namespace: str = "project"):
    resolved = _resolve_memory_workspace(workspace)
    blocks = await CoreMemoryBuilder(
        store=store,
        max_tokens=cfg.memory.core.max_tokens,
    ).rebuild(resolved, namespace=namespace, now_ms=int(time.time() * 1000))
    return {"blocks": [block.model_dump() for block in blocks]}


@app.get("/config/raw")
async def get_config_raw(scope: str = "project"):
    if scope not in ("project", "global"):
        raise HTTPException(
            status_code=400, detail="scope must be 'project' or 'global'"
        )

    if scope == "global":
        paths = [Path(p).expanduser() for p in GLOBAL_CONFIG_PATHS]
    else:
        paths = [Path(p) for p in project_config_paths(cfg.default_worktree)]

    for p in paths:
        if p.is_file():
            raw = p.read_text(encoding="utf-8")
            try:
                import yaml as _yaml

                parsed = _yaml.safe_load(raw)
            except _yaml.YAMLError as exc:
                logger.warning(
                    "Refused to expose invalid raw config because it cannot be safely redacted",
                    event="config.raw.redaction_failed",
                    scope=scope,
                    path=str(p),
                )
                raise HTTPException(
                    status_code=422,
                    detail="Config syntax is invalid and cannot be safely displayed. Edit the file locally.",
                ) from exc
            if parsed is None:
                parsed = {}
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=422, detail="Config must be a mapping")
            redacted, configured_fields = redact_sensitive(parsed)
            content = _yaml.safe_dump(
                redacted,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            return {
                "scope": scope,
                "path": str(p),
                "content": content,
                "exists": True,
                "sensitive_fields_set": configured_fields,
            }

    # No file found — return empty
    preferred = str(paths[0]) if paths else ""
    return {"scope": scope, "path": preferred, "content": "", "exists": False}


@app.put("/config/raw")
async def put_config_raw(body: dict[str, Any]):
    scope = body.get("scope", "project")
    content = body.get("content", "")
    if scope not in ("project", "global"):
        raise HTTPException(
            status_code=400, detail="scope must be 'project' or 'global'"
        )
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")

    # Validate YAML syntax and preserve secrets represented by the redacted placeholder.
    try:
        import yaml as _yaml

        parsed = _yaml.safe_load(content)
        if content.strip() and not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping (dict)")
    except _yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    if scope == "global":
        path = Path(GLOBAL_CONFIG_PATHS[0]).expanduser()
    else:
        paths = project_config_paths(cfg.default_worktree)
        path = Path(paths[0]) if paths else None
        if not path:
            raise HTTPException(
                status_code=400, detail="No project worktree configured"
            )

    parsed = parsed or {}
    existing = _load_yaml_file(str(path)) or {}
    safe_config = restore_redacted_sensitive(parsed, existing)
    save_config(safe_config, str(path))
    logger.info(
        "Configuration file updated",
        event="config.raw.updated",
        scope=scope,
        path=str(path),
    )
    return {"ok": True, "path": str(path), "restart_required": True}


@app.patch("/config")
async def patch_config(body: dict[str, Any]):
    """Partial update: merge into existing project config file."""
    paths = project_config_paths(cfg.default_worktree)
    if not paths:
        raise HTTPException(status_code=400, detail="No project worktree configured")

    config_path = paths[0]  # prefer YAML
    existing = _load_yaml_file(config_path) or {}
    merged = _deep_merge(existing, body)
    save_config(merged, config_path)
    logger.info(
        "Configuration updated",
        event="config.updated",
        scope="project",
        path=config_path,
        updated_fields=sorted(body.keys()),
    )
    return {"ok": True, "path": config_path, "restart_required": True}


@app.post("/config/init")
async def init_config(body: dict[str, Any]):
    """Generate a default config file at the specified scope."""
    scope = body.get("scope", "project")
    if scope not in ("project", "global"):
        raise HTTPException(
            status_code=400, detail="scope must be 'project' or 'global'"
        )

    if scope == "global":
        path = Path(GLOBAL_CONFIG_PATHS[0]).expanduser()
    else:
        paths = project_config_paths(cfg.default_worktree)
        if not paths:
            raise HTTPException(
                status_code=400, detail="No project worktree configured"
            )
        path = Path(paths[0])

    if path.is_file():
        raise HTTPException(
            status_code=409, detail=f"Config file already exists: {path}"
        )

    content = generate_default_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path)}


def _resolve_log_dir() -> Path:
    raw = cfg.logging.dir or "./data/logs"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p


def _validate_date(value: str) -> str:
    date = value.strip()
    if not _DATE_RE.fullmatch(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    return date


def _list_log_files(log_dir: Path) -> list[dict[str, object]]:
    if not log_dir.exists() or not log_dir.is_dir():
        return []
    files: list[dict[str, object]] = []
    for p in log_dir.iterdir():
        if not p.is_file():
            continue
        m = _LOG_FILE_RE.fullmatch(p.name)
        if not m:
            continue
        stat = p.stat()
        files.append(
            {
                "date": m.group(1),
                "name": p.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime * 1000),
            },
        )
    files.sort(key=lambda x: str(x["date"]), reverse=True)
    return files


def _log_file_for_date(log_dir: Path, date: str) -> Path:
    return log_dir / f"nexapilot_{date}.jsonl"


@app.get("/logs/files")
async def list_log_files():
    log_dir = _resolve_log_dir()
    files = _list_log_files(log_dir)
    return {
        "log_dir": str(log_dir),
        "files": files,
        "default_date": files[0]["date"] if files else None,
    }


@app.get("/logs")
async def list_logs(
    date: str,
    offset: int = 0,
    limit: int = 100,
    level: str | None = None,
    event: str | None = None,
    session_id: str | None = None,
    q: str | None = None,
):
    date = _validate_date(date)
    if offset < 0:
        offset = 0
    limit = max(1, min(limit, 500))

    level_norm = (level or "").strip().upper()
    if level_norm and level_norm not in _LOG_LEVELS:
        raise HTTPException(
            status_code=400, detail="level must be one of DEBUG, INFO, WARNING, ERROR"
        )
    event_norm = (event or "").strip().lower()
    session_norm = (session_id or "").strip()
    q_norm = (q or "").strip().lower()

    log_dir = _resolve_log_dir()
    path = _log_file_for_date(log_dir, date)
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail=f"log file not found for date {date}"
        )

    matched: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line_text = line.strip()
            if not line_text:
                continue
            try:
                raw_obj = json.loads(line_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw_obj, dict):
                continue

            item_level = str(raw_obj.get("level") or "").upper()
            item_event = str(raw_obj.get("event") or "")
            item_session = str(raw_obj.get("session_id") or "")
            item_text = json.dumps(
                raw_obj, ensure_ascii=False, separators=(",", ":")
            ).lower()

            if level_norm and item_level != level_norm:
                continue
            if event_norm and event_norm not in item_event.lower():
                continue
            if session_norm and item_session != session_norm:
                continue
            if q_norm and q_norm not in item_text:
                continue

            matched.append(
                {
                    "line_no": i,
                    "ts": raw_obj.get("ts"),
                    "level": raw_obj.get("level"),
                    "event": raw_obj.get("event"),
                    "session_id": raw_obj.get("session_id"),
                    "message": raw_obj.get("message"),
                    "module": raw_obj.get("module"),
                    "function": raw_obj.get("function"),
                    "raw": raw_obj,
                },
            )

    matched.reverse()  # newest first
    total = len(matched)
    page = matched[offset : offset + limit]
    return {
        "date": date,
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": (offset + limit) < total,
        "items": page,
    }


def _resolve_project_root(root_path: str) -> Path:
    raw = root_path.strip()
    if not raw or not os.path.isabs(raw):
        raise HTTPException(status_code=400, detail="project root must be an absolute path")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="project root must be an existing directory")
    return root


@app.post("/projects")
async def create_project(body: CreateProjectRequest):
    root = _resolve_project_root(body.root_path)
    name = body.name.strip() or root.name or str(root)
    now = int(time.time() * 1000)
    project = Project(
        id=str(uuid4()),
        name=name,
        root_path=str(root),
        created_at=now,
        updated_at=now,
        last_opened_at=now,
    )
    try:
        return await store.create_project(project)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="a project already exists for this root"
        ) from exc


@app.get("/projects")
async def list_projects(limit: int = 100, offset: int = 0):
    projects = await store.list_projects(
        limit=min(max(limit, 1), 500), offset=max(offset, 0)
    )
    items = []
    for project in projects:
        threads = await store.list_sessions(
            limit=10_000,
            offset=0,
            include_children=False,
            project_id=project.id,
        )
        payload = project.model_dump()
        payload["thread_count"] = len(threads)
        payload["last_thread_at"] = max(
            (thread.updated_at for thread in threads), default=None
        )
        items.append(payload)
    return {"projects": items}


@app.get("/projects/{project_id}")
async def get_project(project_id: str):
    try:
        return await store.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/projects/{project_id}/threads")
async def get_project_threads(
    project_id: str, include_children: bool = False, limit: int = 100, offset: int = 0
):
    try:
        await store.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    threads = await store.list_sessions(
        limit=min(max(limit, 1), 500),
        offset=max(offset, 0),
        include_children=include_children,
        project_id=project_id,
    )
    return {"threads": [thread.model_dump() for thread in threads]}


@app.patch("/projects/{project_id}")
async def rename_project(project_id: str, body: RenameProjectRequest):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        return await store.update_project_name(project_id, name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/projects/{project_id}/open")
async def open_project(project_id: str):
    try:
        return await store.touch_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    try:
        await store.delete_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "project_id": project_id}


@app.post("/sessions")
async def create_session(body: CreateSessionRequest):
    requested_runtime = body.runtime or SessionRuntime(backend="local")
    runtime_backend = requested_runtime.backend

    raw_worktree = body.worktree.strip()
    title = body.title.strip() or "New session"
    project: Project | None = None
    if body.project_id:
        if runtime_backend != "local":
            raise HTTPException(
                status_code=400,
                detail="projects currently support the local runtime only",
            )
        try:
            project = await store.get_project(body.project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if raw_worktree and Path(raw_worktree).resolve() != Path(project.root_path):
            raise HTTPException(
                status_code=409,
                detail="worktree must match the selected project root",
            )
        raw_worktree = project.root_path
    if runtime_backend == "daytona":
        default_workspace = cfg.daytona.default_workspace or "/workspace"
        # When frontend toggles from local->daytona, it may still carry local default_worktree.
        # In that case treat it as "unset" and fall back to remote default workspace.
        if raw_worktree and raw_worktree != cfg.default_worktree:
            worktree = raw_worktree
        else:
            worktree = default_workspace
        cwd = body.cwd.strip() or worktree
        if not worktree.startswith("/") or not cwd.startswith("/"):
            raise HTTPException(
                status_code=400,
                detail="daytona worktree/cwd must be absolute remote paths",
            )
        runtime = SessionRuntime(
            backend="daytona",
            daytona=DaytonaRuntimeConfig(
                sandbox_id=requested_runtime.daytona.sandbox_id
                if requested_runtime.daytona
                else None,
                sandbox_name=requested_runtime.daytona.sandbox_name
                if requested_runtime.daytona
                else None,
            ),
        )
    else:
        worktree = raw_worktree
        if not worktree or not os.path.isabs(worktree):
            raise HTTPException(
                status_code=400, detail="worktree must be an absolute path"
            )
        cwd = body.cwd.strip() or worktree
        if project is not None:
            resolved_cwd = Path(cwd).expanduser().resolve()
            if not resolved_cwd.is_relative_to(Path(project.root_path)):
                raise HTTPException(
                    status_code=400,
                    detail="cwd must stay inside the selected project root",
                )
            cwd = str(resolved_cwd)
        runtime = SessionRuntime(backend="local")

    permission_rules = (
        [PermissionRule.model_validate(x) for x in body.permission_rules]
        if body.permission_rules
        else _default_rules()
    )
    now = int(time.time() * 1000)
    session_id = str(uuid4())
    s = Session(
        id=session_id,
        title=title,
        worktree=worktree,
        cwd=cwd,
        created_at=now,
        updated_at=now,
        permission_rules=permission_rules,
        runtime=runtime,
        kind="primary",
        agent_name="primary",
        root_session_id=session_id,
        project_id=project.id if project is not None else None,
    )
    await store.create_session(s)
    if project is not None:
        await store.touch_project(project.id)
    if runtime_backend == "local":
        try:
            await memory_service.archive_previous_session_for_new_session(s)
        except Exception as exc:
            logger.warning(
                "Automatic memory archive failed during session creation",
                event="memory.archive.error",
                session_id=s.id,
                worktree=s.worktree,
                error=str(exc),
            )
        await memory_service.ensure_worktree(s.worktree)
    if runtime_backend == "daytona":
        try:
            s = await daytona_manager.ensure_session_runtime_async(s)
        except DaytonaUnavailableError as exc:
            await store.delete_session(s.id)
            raise HTTPException(status_code=503, detail=str(exc))
        except DaytonaOperationError as exc:
            await store.delete_session(s.id)
            msg = str(exc)
            if "not found" in msg.lower():
                raise HTTPException(status_code=400, detail=msg)
            raise HTTPException(status_code=502, detail=msg)

    await bus.publish(
        Event(
            type="session.created",
            properties={"session_id": s.id, "info": s.model_dump()},
        )
    )
    return s


@app.get("/sessions")
async def list_sessions(
    limit: int = 50,
    offset: int = 0,
    include_children: bool = False,
    parent_session_id: str | None = None,
    project_id: str | None = None,
):
    """List all sessions, ordered by updated_at desc."""
    sessions = await store.list_sessions(
        limit=limit,
        offset=offset,
        include_children=include_children,
        parent_session_id=parent_session_id,
        project_id=project_id,
    )
    refreshed: list[Session] = []
    for s in sessions:
        refreshed.append(await _refresh_daytona_runtime_metadata(s))
    sessions = refreshed
    return {"sessions": [s.model_dump() for s in sessions]}


@app.patch("/sessions/{session_id}")
async def rename_session(session_id: str, body: RenameSessionRequest):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    await _get_session_or_404(session_id)
    session = await store.update_session_title(session_id, title)
    await bus.publish(
        Event(
            type="session.updated",
            properties={"session_id": session_id, "info": session.model_dump()},
        )
    )
    return session


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    session = await _get_session_or_404(session_id)
    active_workspaces = [
        workspace
        for workspace in await store.list_agent_workspaces(
            root_session_id=session.root_session_id or session.id
        )
        if workspace["status"] not in {"released", "missing"}
        and (
            session.kind == "primary"
            or workspace["child_session_id"] == session.id
        )
    ]
    if active_workspaces:
        raise HTTPException(
            status_code=409,
            detail="release retained agent workspaces before deleting the session",
        )
    await artifact_store.delete_session_content(session_id)
    await store.delete_session(session_id)
    await bus.publish(
        Event(
            type="session.deleted",
            properties={"session_id": session_id, "info": session.model_dump()},
        ),
    )
    return {"ok": True, "session_id": session_id}


@app.post("/sessions/{session_id}/messages")
async def add_message(session_id: str, body: AddMessageRequest):
    session = await store.get_session(session_id)
    message_id = await _add_user_message(session, body.text, source="api")
    return {"message_id": message_id}


@app.post("/sessions/{session_id}/run")
async def run_session(session_id: str):
    session = await store.get_session(session_id)
    try:
        msg_id, trace_id = await _run_agent_once(session, source="api")
    except DaytonaUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except DaytonaOperationError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    run = await store.get_run_by_assistant_message(msg_id)
    result = {"run_id": run.id, "assistant_message_id": msg_id}
    if trace_id:
        result["trace_id"] = trace_id
        result["trace_url"] = f"{cfg.langfuse.base_url}/trace/{trace_id}"
    return result


@app.get("/sessions/{session_id}/runs")
async def list_session_runs(session_id: str, limit: int = 50, offset: int = 0):
    await _get_session_or_404(session_id)
    runs = await store.list_runs(
        session_id, limit=min(max(limit, 1), 200), offset=max(offset, 0)
    )
    return {"runs": [run.model_dump() for run in runs]}


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    try:
        return await store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/runs/{run_id}/messages")
async def list_run_messages(run_id: str):
    try:
        run = await store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await store.list_messages(run.session_id, run_id=run.id)


@app.get("/runs/{run_id}/steps")
async def list_run_steps(run_id: str):
    try:
        await store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    steps = await store.list_run_steps(run_id)
    return {"steps": [step.model_dump() for step in steps]}


@app.get("/runs/{run_id}/operations")
async def list_run_operations(run_id: str):
    try:
        await store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"operations": await store.list_run_tool_operations(run_id)}


@app.get("/runs/{run_id}/workspace")
async def get_run_workspace(run_id: str):
    try:
        return await run_workspace.build(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/runs/{run_id}/artifacts")
async def list_run_artifacts(run_id: str):
    try:
        await store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    artifacts = await store.list_artifacts(run_id=run_id)
    return {
        "artifacts": [run_workspace.public_artifact(item) for item in artifacts]
    }


@app.get("/runs/{run_id}/feedback")
async def get_run_feedback(run_id: str):
    try:
        await store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    feedback = await store.get_run_feedback(run_id)
    return {"feedback": feedback.model_dump() if feedback is not None else None}


@app.post("/runs/{run_id}/feedback")
async def submit_run_feedback(run_id: str, body: FeedbackSubmission):
    try:
        feedback, candidate, created = await feedback_service.submit(run_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        await bus.publish(
            Event(
                type="evaluation.feedback.created",
                properties={
                    "session_id": feedback.session_id,
                    "run_id": feedback.run_id,
                    "feedback_id": feedback.id,
                    "rating": feedback.rating,
                    "candidate_id": candidate.id if candidate is not None else None,
                },
            )
        )
    return {
        "feedback": feedback.model_dump(),
        "candidate": candidate.model_dump() if candidate is not None else None,
        "created": created,
    }


@app.get("/evaluation/feedback")
async def list_evaluation_feedback(
    session_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    if session_id is not None:
        await _get_session_or_404(session_id)
    feedback = await store.list_run_feedback(
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    return {"feedback": [item.model_dump() for item in feedback]}


@app.get("/evaluation/candidates")
async def list_evaluation_candidates(
    status: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    if session_id is not None:
        await _get_session_or_404(session_id)
    try:
        candidates = await store.list_eval_candidates(
            status=status,
            session_id=session_id,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"candidates": [item.model_dump() for item in candidates]}


@app.post("/evaluation/candidates/{candidate_id}/review")
async def review_evaluation_candidate(
    candidate_id: str,
    body: EvalCandidateReview,
):
    note, _ = redact_feedback_text(body.note, limit=2_000)
    try:
        candidate = await store.review_eval_candidate(
            candidate_id,
            decision=body.decision,
            note=note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await bus.publish(
        Event(
            type="evaluation.candidate.reviewed",
            properties={
                "session_id": candidate.session_id,
                "run_id": candidate.run_id,
                "candidate_id": candidate.id,
                "status": candidate.status,
            },
        )
    )
    return {
        "candidate": candidate.model_dump(),
        "baseline_promoted": False,
        "notice": "review accepts a bad-case candidate only; baseline promotion remains a separate manual action",
    }


@app.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    try:
        artifact = await store.get_artifact(artifact_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return run_workspace.public_artifact(artifact)


async def _artifact_response(artifact_id: str, *, download: bool) -> Response:
    try:
        artifact, content = await artifact_store.read(artifact_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ArtifactIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    headers = {
        "ETag": f'"sha256:{artifact.sha256}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if download:
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', artifact.name) or "artifact.bin"
        headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return Response(content=content, media_type=artifact.media_type, headers=headers)


@app.get("/artifacts/{artifact_id}/content")
async def preview_artifact(artifact_id: str):
    return await _artifact_response(artifact_id, download=False)


@app.get("/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str):
    return await _artifact_response(artifact_id, download=True)


@app.get("/runs/{run_id}/llm-calls")
async def list_run_llm_calls(run_id: str):
    try:
        await store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    calls = await store.list_run_llm_calls(run_id)
    return {
        "calls": calls,
        "totals": await store.get_run_llm_totals(run_id),
    }


@app.get("/llm-calls/{call_id}/attempts")
async def list_llm_call_attempts(call_id: str):
    try:
        call = await store.get_llm_call(call_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "call": call,
        "attempts": await store.list_llm_call_attempts(call_id),
    }


@app.get("/providers/capabilities")
async def get_provider_capabilities():
    resolver = CapabilityResolver(cfg.openai)
    return {
        "configured_transport": cfg.openai.transport,
        "capabilities": resolver.resolve().to_dict(),
        "model_gateway": {
            "enabled": cfg.model_gateway.enabled,
            "default_route": cfg.model_gateway.default_route,
            "providers": sorted(cfg.model_gateway.providers),
            "models": sorted(cfg.model_gateway.models),
            "routes": {
                name: list(route.candidates)
                for name, route in cfg.model_gateway.routes.items()
            },
        },
    }


@app.get("/providers/status")
async def get_provider_status():
    capabilities = CapabilityResolver(cfg.openai).resolve()
    return {
        "provider": capabilities.provider,
        "model": cfg.openai.model,
        "configured_transport": cfg.openai.transport,
        "gateway_enabled": cfg.openai.resilience.enabled,
        "multi_model_enabled": cfg.model_gateway.enabled,
        "default_route": cfg.model_gateway.default_route,
        "capability_profile": capabilities.profile,
        "circuits": await store.list_provider_circuits(),
    }


@app.get("/agents")
async def list_agent_definitions():
    agents = []
    for agent in agent_service.registry.list():
        agents.append(
            {
                "name": agent.name,
                "mode": agent.mode,
                "description": agent.description,
                "capabilities": sorted(agent.capabilities),
                "tools": (
                    sorted(agent.tool_allowlist)
                    if agent.tool_allowlist is not None
                    else None
                ),
                "permissions": [
                    rule.model_dump() for rule in agent.permission_profile
                ],
                "limits": {
                    "max_turns": agent.limits.max_turns,
                    "max_tool_calls": agent.limits.max_tool_calls,
                    "max_wall_time_ms": agent.limits.max_wall_time_ms,
                    "max_concurrency": agent.limits.max_concurrency,
                    "max_input_tokens": agent.limits.max_input_tokens,
                },
                "model": (
                    agent.model_override.model_dump()
                    if agent.model_override
                    else None
                ),
                "workspace": {
                    "mode": agent.workspace.mode,
                    "cleanup": agent.workspace.cleanup,
                },
                "source": agent.source,
            }
        )
    return {"agents": agents}


@app.get("/agent-workspaces")
async def list_agent_workspaces(root_session_id: str | None = None):
    return {
        "workspaces": await store.list_agent_workspaces(
            root_session_id=root_session_id
        )
    }


@app.get("/agent-workspaces/{workspace_id}")
async def get_agent_workspace(workspace_id: str):
    try:
        return await agent_service.workspace_manager.inspect(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentWorkspaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/agent-workspaces/{workspace_id}/release")
async def release_agent_workspace(
    workspace_id: str, body: AgentWorkspaceReleaseRequest
):
    try:
        return await agent_service.release_workspace(
            workspace_id, force=body.force
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentWorkspaceDirty as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (AgentWorkspaceError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    try:
        current = await store.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    requested = await store.request_run_cancel(run_id=run_id)
    if requested is None:
        return {
            "ok": True,
            "run": current.model_dump(),
            "already_terminal": True,
        }
    await interrupt.interrupt(current.session_id, reason="user_cancelled")
    await perm.cancel_session(current.session_id, reason="user_cancelled")
    await bus.publish(
        Event(
            type="run.state.changed",
            properties={
                "session_id": current.session_id,
                "run_id": current.id,
                "from": current.status,
                "to": requested.status,
                "revision": requested.revision,
                "reason": "user_cancelled",
            },
        )
    )
    return {"ok": True, "run": requested.model_dump(), "already_terminal": False}


@app.post("/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    """Interrupt a running session."""
    await store.get_session(session_id)  # Validate session exists
    run = await store.request_run_cancel(
        session_id=session_id, reason="user_cancelled"
    )
    await interrupt.interrupt(session_id, reason="user_cancelled")
    await perm.cancel_session(session_id, reason="user_cancelled")
    await bus.publish(
        Event(
            type="session.interrupted",
            properties={"session_id": session_id, "reason": "user_cancelled"},
        ),
    )
    return {"ok": True, "run": run.model_dump() if run else None}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    s = await store.get_session(session_id)
    s = await _refresh_daytona_runtime_metadata(s)
    return s


@app.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str):
    await store.get_session(session_id)
    return await store.list_messages(session_id)


@app.get("/sessions/{session_id}/todos")
async def get_todos(session_id: str):
    """Get the current todo list for a session."""
    await store.get_session(session_id)  # Validate session exists
    todos = await store.get_todos(session_id)
    return {"session_id": session_id, "todos": [t.model_dump() for t in todos]}


@app.post("/sessions/{session_id}/goals")
async def create_goal(session_id: str, body: CreateGoalRequest):
    return await task_runtime.create_goal(session_id, body)


@app.get("/sessions/{session_id}/goals")
async def list_goals(session_id: str):
    await _get_session_or_404(session_id)
    goals = await task_runtime.list_goals(session_id)
    return {"goals": [goal.model_dump() for goal in goals]}


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: str):
    return await task_runtime.get_goal(goal_id)


@app.get("/goals/{goal_id}/events")
async def list_goal_events(goal_id: str):
    events = await task_runtime.list_events(goal_id)
    return {"events": [event.model_dump() for event in events]}


@app.post("/goals/{goal_id}/plans")
async def create_goal_plan(goal_id: str, body: CreatePlanRequest):
    return await task_runtime.create_plan(goal_id, body)


@app.post("/goals/{goal_id}/pause")
async def pause_goal(goal_id: str, body: RevisionActionRequest):
    return await task_runtime.set_goal_paused(
        goal_id,
        paused=True,
        expected_revision=body.expected_revision,
        actor=body.actor,
        reason=body.reason,
    )


@app.post("/goals/{goal_id}/resume")
async def resume_goal(goal_id: str, body: RevisionActionRequest):
    return await task_runtime.set_goal_paused(
        goal_id,
        paused=False,
        expected_revision=body.expected_revision,
        actor=body.actor,
        reason=body.reason,
    )


@app.get("/plans/{plan_id}/ready-tasks")
async def list_ready_tasks(plan_id: str):
    tasks = await task_runtime.ready_tasks(plan_id)
    return {"tasks": [task.model_dump() for task in tasks]}


@app.post("/plans/{plan_id}/reorder")
async def reorder_plan(plan_id: str, body: ReorderPlanRequest):
    return await task_runtime.reorder_plan(
        plan_id,
        task_ids=body.task_ids,
        expected_revision=body.expected_revision,
        actor=body.actor,
        reason=body.reason,
    )


@app.post("/tasks/{task_id}/transition")
async def transition_task(task_id: str, body: TaskTransitionRequest):
    return await task_runtime.transition_task(
        task_id,
        target=body.status,
        expected_revision=body.expected_revision,
        actor=body.actor,
        reason=body.reason,
        run_id=body.run_id,
        result_payload=body.result,
        error_payload=body.error,
        retry_delay_ms=body.retry_delay_ms,
    )


@app.post("/tasks/{task_id}/takeover")
async def takeover_task(task_id: str, body: TaskTakeoverRequest):
    return await task_runtime.takeover_task(
        task_id,
        human=True,
        expected_revision=body.expected_revision,
        actor=body.actor,
        assignee=body.assignee,
        reason=body.reason,
    )


@app.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, body: RevisionActionRequest):
    return await task_runtime.transition_task(
        task_id,
        target="ready",
        expected_revision=body.expected_revision,
        actor=body.actor,
        reason=body.reason or "manual_retry",
    )


@app.post("/tasks/{task_id}/release")
async def release_task(task_id: str, body: TaskReleaseRequest):
    return await task_runtime.takeover_task(
        task_id,
        human=False,
        expected_revision=body.expected_revision,
        actor=body.actor,
        assignee=None,
        reason=body.reason,
    )


@app.post("/cronjobs")
async def create_cron_job(body: CreateCronJobRequest):
    service = _require_cron_service()
    await _get_session_or_404(body.session_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    now = int(time.time() * 1000)
    job = CronJob(
        id=str(uuid4()),
        name=name,
        session_id=body.session_id,
        enabled=body.enabled,
        schedule=body.schedule,
        payload={"kind": "agent_turn", "message": body.message},
        created_at_ms=now,
        updated_at_ms=now,
        delete_after_run=body.delete_after_run,
    )
    try:
        return await service.create_job(job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/cronjobs")
async def list_cron_jobs(session_id: str | None = None, include_disabled: bool = True):
    jobs = await store.list_cron_jobs(
        session_id=session_id, include_disabled=include_disabled
    )
    return {"jobs": [j.model_dump() for j in jobs]}


@app.get("/cronjobs/status")
async def cron_status():
    service = _require_cron_service()
    return await service.status()


@app.get("/cronjobs/{job_id}")
async def get_cron_job(job_id: str):
    try:
        return await store.get_cron_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]))


@app.delete("/cronjobs/{job_id}")
async def delete_cron_job(job_id: str):
    service = _require_cron_service()
    deleted = await service.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"cron job not found: {job_id}")
    return {"ok": True, "job_id": job_id}


@app.post("/cronjobs/{job_id}/enabled")
async def set_cron_job_enabled(job_id: str, body: CronJobEnabledRequest):
    service = _require_cron_service()
    try:
        return await service.set_job_enabled(job_id, enabled=body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/cronjobs/{job_id}/run")
async def run_cron_job(job_id: str, body: CronJobRunRequest | None = None):
    service = _require_cron_service()
    force = bool(body.force) if body else False
    try:
        ran = await service.run_job(job_id, force=force)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]))
    if not ran:
        raise HTTPException(
            status_code=409,
            detail="cron job is disabled; pass force=true to run manually",
        )
    return {"ok": True, "job_id": job_id}


@app.get("/cronjobs/{job_id}/runs")
async def list_cron_job_runs(job_id: str, limit: int = 50):
    try:
        await store.get_cron_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0]))
    runs = await store.list_cron_job_runs(job_id, limit=max(1, min(limit, 200)))
    return {"job_id": job_id, "runs": [r.model_dump() for r in runs]}


@app.get("/permissions/pending")
async def pending_permissions(session_id: str):
    await store.get_session(session_id)
    return await store.list_pending_permission_requests(session_id)


async def _scope_event_properties(
    event: Event,
    *,
    target_session_id: str | None,
    target_root_id: str | None,
    scope: str,
    run_sessions: dict[str, str | None],
    session_contexts: dict[str, Session | None],
) -> dict[str, Any] | None:
    properties = dict(event.properties)
    info = properties.get("info") or {}
    part = properties.get("part") or {}
    sid = (
        properties.get("session_id")
        or info.get("session_id")
        or part.get("session_id")
    )
    run_id = properties.get("run_id") or info.get("run_id")
    if sid is None and run_id:
        if run_id not in run_sessions:
            try:
                run_sessions[run_id] = (await store.get_run(run_id)).session_id
            except KeyError:
                run_sessions[run_id] = None
        sid = run_sessions[run_id]

    event_session: Session | None = None
    if sid:
        if sid not in session_contexts:
            try:
                session_contexts[sid] = await store.get_session(sid)
            except KeyError:
                session_contexts[sid] = None
        event_session = session_contexts[sid]

    if target_session_id:
        if event_session is None:
            # Never leak unscoped provider/runtime events into a session stream.
            return None
        event_root_id = event_session.root_session_id or event_session.id
        if scope == "session" and event_session.id != target_session_id:
            return None
        if scope == "tree" and event_root_id != target_root_id:
            return None

    if event_session is not None:
        properties.setdefault("session_id", event_session.id)
        properties.setdefault(
            "root_session_id",
            event_session.root_session_id or event_session.id,
        )
        properties.setdefault("parent_session_id", event_session.parent_session_id)
        properties.setdefault("parent_tool_call_id", event_session.parent_tool_call_id)
        properties.setdefault("agent", event_session.agent_name)
        properties.setdefault("session_kind", event_session.kind)
    return properties


@app.get("/events")
async def events(session_id: str | None = None, scope: str = "session"):
    if scope not in {"session", "tree"}:
        raise HTTPException(status_code=400, detail="scope must be 'session' or 'tree'")
    target_root_id: str | None = None
    if session_id:
        try:
            target = await store.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0]))
        target_root_id = target.root_session_id or target.id

    async def gen():
        run_sessions: dict[str, str | None] = {}
        session_contexts: dict[str, Session | None] = {}
        async for e in bus.subscribe("*"):
            properties = await _scope_event_properties(
                e,
                target_session_id=session_id,
                target_root_id=target_root_id,
                scope=scope,
                run_sessions=run_sessions,
                session_contexts=session_contexts,
            )
            if properties is None:
                continue
            yield f"data: {json.dumps({'type': e.type, 'properties': properties})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/channels/status")
async def channels_status():
    runtime_cfg = _load_effective_config()
    return {
        "enabled_channels": channel_manager.enabled_channels,
        "channels": channel_manager.get_status(),
        "configured": {
            "feishu": _serialize_feishu_config(runtime_cfg.channels.feishu),
        },
        "bridge_running": bool(channel_bridge and channel_bridge.is_running),
        "queue": {
            "inbound": channel_bus.inbound_size,
            "outbound": channel_bus.outbound_size,
        },
    }


@app.get("/channels/config")
async def channels_config():
    runtime_cfg = _load_effective_config()
    return {
        "channels": {
            "feishu": _serialize_feishu_config(runtime_cfg.channels.feishu),
        }
    }


@app.put("/channels/config/feishu")
async def update_feishu_channel_config(body: dict[str, Any]):
    paths = project_config_paths(cfg.default_worktree)
    if not paths:
        raise HTTPException(status_code=400, detail="No project worktree configured")

    existing_cfg = _load_effective_config()
    existing = _load_yaml_file(paths[0]) or {}
    existing_feishu = existing_cfg.channels.feishu

    keep_existing_secret = bool(body.get("keep_existing_secret", True))
    app_secret = str(body.get("app_secret", "") or "").strip()
    encrypt_key = str(body.get("encrypt_key", "") or "").strip()
    verification_token = str(body.get("verification_token", "") or "").strip()

    if keep_existing_secret:
        if not app_secret:
            app_secret = existing_feishu.app_secret
        if not encrypt_key:
            encrypt_key = existing_feishu.encrypt_key
        if not verification_token:
            verification_token = existing_feishu.verification_token

    permission_mode_raw = (
        str(
            body.get(
                "permission_mode", getattr(existing_feishu, "permission_mode", "deny")
            )
            or "deny"
        )
        .strip()
        .lower()
    )
    if permission_mode_raw not in ("deny", "allow", "commands"):
        raise HTTPException(
            status_code=400,
            detail="permission_mode must be one of: deny, allow, commands",
        )

    feishu_patch = {
        "enabled": bool(body.get("enabled", existing_feishu.enabled)),
        "app_id": str(body.get("app_id", existing_feishu.app_id) or "").strip(),
        "app_secret": app_secret,
        "encrypt_key": encrypt_key,
        "verification_token": verification_token,
        "allow_from": _parse_allow_from(
            body.get("allow_from", existing_feishu.allow_from)
        ),
        "permission_mode": permission_mode_raw,
        "allowed_bash_commands": _parse_line_list(
            body.get(
                "allowed_bash_commands",
                getattr(existing_feishu, "allowed_bash_commands", []),
            )
        ),
    }

    merged = _deep_merge(existing, {"channels": {"feishu": feishu_patch}})
    save_config(merged, paths[0])

    runtime_cfg = _load_effective_config()
    await _reload_channels_runtime(runtime_cfg)
    logger.info(
        "Feishu channel config updated",
        event="channel.config.updated",
        channel="feishu",
        enabled=feishu_patch["enabled"],
        allow_from_count=len(feishu_patch["allow_from"]),
        permission_mode=feishu_patch["permission_mode"],
        allowed_bash_commands_count=len(feishu_patch["allowed_bash_commands"]),
    )
    return {
        "ok": True,
        "restart_required": False,
        "channels": {"feishu": _serialize_feishu_config(runtime_cfg.channels.feishu)},
        "runtime_status": channel_manager.get_status(),
    }


@app.post("/channels/{name}/connect")
async def connect_channel(name: str):
    try:
        await channel_manager.connect_channel(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"channel not found: {name}")
    return {
        "ok": True,
        "channel": name,
        "status": channel_manager.get_status().get(name, {}),
    }


@app.post("/channels/{name}/disconnect")
async def disconnect_channel(name: str):
    try:
        await channel_manager.disconnect_channel(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"channel not found: {name}")
    return {
        "ok": True,
        "channel": name,
        "status": channel_manager.get_status().get(name, {}),
    }


@app.post("/channels/{name}/test")
async def test_channel(name: str):
    try:
        result = await channel_manager.test_channel(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"channel not found: {name}")
    return result


@app.post("/permissions/{request_id}/reply")
async def reply_permission(request_id: str, body: PermissionReply):
    await perm.reply(request_id, body)
    return {"ok": True}


# -- Skills endpoints --


@app.get("/skills")
async def list_skills(worktree: str | None = None):
    wt = (worktree or "").strip() or cfg.default_worktree
    loader = SkillLoader(worktree=wt, cwd=wt)
    skills = loader.list_skills()
    return {
        "worktree": wt,
        "skills": [
            {"name": s.name, "description": s.description, "path": s.path, "dir": s.dir}
            for s in skills
        ],
    }


@app.get("/skills/{name}")
async def get_skill(name: str, worktree: str | None = None):
    wt = (worktree or "").strip() or cfg.default_worktree
    loader = SkillLoader(worktree=wt, cwd=wt)
    skill = loader.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    files = loader.sample_files(name)
    return {
        "name": skill.name,
        "description": skill.description,
        "path": skill.path,
        "dir": skill.dir,
        "body": skill.body,
        "files": files,
    }


# -- MCP endpoints --


@app.get("/mcp/status")
async def mcp_status():
    statuses = mcp_manager.status()
    return {
        "servers": [
            {
                "name": s.name,
                "status": s.status,
                "error": s.error,
                "tool_count": s.tool_count,
            }
            for s in statuses
        ]
    }


@app.get("/mcp/tools")
async def mcp_tools():
    tools = await mcp_manager.list_tools()
    return {
        "tools": [
            {
                "server": t.server_name,
                "name": t.tool_name,
                "namespaced_name": t.namespaced_name,
                "description": t.description,
            }
            for t in tools
        ]
    }


@app.post("/mcp/{name}/connect")
async def mcp_connect(name: str):
    try:
        await mcp_manager.connect(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown MCP server: {name}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.post("/mcp/{name}/disconnect")
async def mcp_disconnect(name: str):
    await mcp_manager.disconnect(name)
    return {"ok": True}


@app.post("/mcp/servers")
async def mcp_add_server(body: dict[str, Any]):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    config_raw = body.get("config", {})
    if not isinstance(config_raw, dict):
        raise HTTPException(status_code=400, detail="config must be a dict")

    # Determine type from config
    command = config_raw.get("command", "")
    url = config_raw.get("url", "")
    if not command and not url:
        raise HTTPException(
            status_code=400, detail="config must have 'command' or 'url'"
        )

    if command:
        cfg_obj = MCPServerConfig(
            name=name,
            type="local",
            command=command,
            args=list(config_raw.get("args", [])),
            env=dict(config_raw.get("env", {})),
            enabled=config_raw.get("enabled", True),
            timeout=int(config_raw.get("timeout", 30)),
            source="api",
            transport="stdio",
        )
    else:
        transport = config_raw.get("transport", "streamable-http")
        if transport not in ("stdio", "sse", "streamable-http"):
            transport = "streamable-http"
        cfg_obj = MCPServerConfig(
            name=name,
            type="remote",
            url=url,
            headers=dict(config_raw.get("headers", {})),
            enabled=config_raw.get("enabled", True),
            timeout=int(config_raw.get("timeout", 30)),
            source="api",
            transport=transport,
        )

    try:
        await mcp_manager.add_server(name, cfg_obj)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "name": name}


# -- KB endpoints --


def _require_kb():
    if kb_client is None:
        raise HTTPException(status_code=404, detail="Knowledge base is not configured")
    return kb_client


@app.get("/kb/config")
async def kb_config():
    logger.debug("KB config requested", event="api.kb.config")
    return {
        "enabled": kb_client is not None,
        "backend": cfg.kb.backend if kb_client else "none",
        "vlm_available": vlm_client is not None,
        "vlm_backend": cfg.vlm.backend if vlm_client else "none",
    }


@app.post("/kb/documents/upload")
async def kb_upload_file(file: UploadFile, use_vlm: bool = False):
    kb = _require_kb()
    file_bytes = await file.read()
    filename = file.filename or "upload"
    logger.info(
        "API: KB upload file",
        event="api.kb.upload",
        filename=filename,
        size=len(file_bytes),
        use_vlm=use_vlm,
    )

    if use_vlm:
        if vlm_client is None:
            raise HTTPException(status_code=400, detail="VLM parser is not configured")
        job_id = await vlm_client.submit(file_bytes, filename)
        parse_result = await vlm_client.wait_for_result(job_id)
        text = "\n\n".join(parse_result.markdown_pages)
        logger.info(
            "API: VLM parse done for upload",
            event="api.kb.upload.vlm_done",
            filename=filename,
            pages=len(parse_result.markdown_pages),
        )
        # Upload the VLM-parsed markdown as a .md file
        md_filename = (
            filename.rsplit(".", 1)[0] + ".md" if "." in filename else filename + ".md"
        )
        file_bytes = text.encode("utf-8")
        filename = md_filename

    result = await kb.upload_file(file_bytes, filename)

    return result.model_dump()


@app.delete("/kb/documents")
async def kb_delete_documents(body: dict[str, Any]):
    kb = _require_kb()
    doc_ids = body.get("doc_ids", [])
    if not doc_ids or not isinstance(doc_ids, list):
        raise HTTPException(status_code=400, detail="doc_ids list is required")
    logger.info("API: KB delete documents", event="api.kb.delete", count=len(doc_ids))
    return await kb.delete_documents(doc_ids)


@app.post("/kb/documents/list")
async def kb_list_documents(body: dict[str, Any]):
    kb = _require_kb()
    page = int(body.get("page", 1))
    page_size = int(body.get("page_size", 20))
    status_filter = body.get("status_filter")
    logger.debug(
        "API: KB list documents",
        event="api.kb.list",
        page=page,
        page_size=page_size,
        status_filter=status_filter,
    )
    result = await kb.list_documents(
        page=page, page_size=page_size, status_filter=status_filter
    )
    return result.model_dump()


@app.get("/kb/pipeline/status")
async def kb_pipeline_status():
    kb = _require_kb()
    result = await kb.get_pipeline_status()
    return result.model_dump()


@app.get("/kb/status_counts")
async def kb_status_counts():
    kb = _require_kb()
    result = await kb.get_status_counts()
    return result.model_dump()


@app.post("/kb/query")
async def kb_query(body: dict[str, Any]):
    kb = _require_kb()
    query = str(body.get("query", "")).strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    top_k = int(body.get("top_k", 10))
    logger.info("API: KB query", event="api.kb.query", query=query, top_k=top_k)
    result = await kb.query(query=query, top_k=top_k)
    logger.info(
        "API: KB query done",
        event="api.kb.query.done",
        entities=len(result.entities),
        relationships=len(result.relationships),
        chunks=len(result.chunks),
    )
    return result.model_dump()


@app.post("/kb/vlm/parse")
async def kb_vlm_parse(file: UploadFile):
    """Parse a file using VLM and return markdown via SSE progress stream."""
    if vlm_client is None:
        raise HTTPException(status_code=404, detail="VLM parser is not configured")

    file_bytes = await file.read()
    filename = file.filename or "upload"
    logger.info(
        "API: VLM parse requested",
        event="api.vlm.parse",
        filename=filename,
        size=len(file_bytes),
    )

    async def stream():
        import json as _json

        try:
            job_id = await vlm_client.submit(file_bytes, filename)
            yield f"data: {_json.dumps({'state': 'submitted', 'job_id': job_id})}\n\n"

            result = await vlm_client.wait_for_result(
                job_id,
                on_progress=None,
            )
            combined = "\n\n".join(result.markdown_pages)
            logger.info(
                "API: VLM parse completed",
                event="api.vlm.parse.done",
                filename=filename,
                pages=len(result.markdown_pages),
            )
            yield f"data: {_json.dumps({'state': 'done', 'markdown': combined, 'page_count': len(result.markdown_pages)})}\n\n"
        except Exception as exc:
            logger.error(
                "API: VLM parse failed",
                event="api.vlm.parse.error",
                filename=filename,
                error=str(exc),
            )
            yield f"data: {_json.dumps({'state': 'failed', 'error': str(exc)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
