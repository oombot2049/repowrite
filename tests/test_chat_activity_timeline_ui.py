from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "nexapilot" / "web" / "index.html"
PARTS_STYLES = ROOT / "src" / "nexapilot" / "web" / "styles" / "parts.css"


def test_chat_groups_operational_parts_into_user_facing_activity() -> None:
    document = INDEX.read_text(encoding="utf-8")

    assert "function messagePartBlocks(parts)" in document
    assert "function ActivityGroup({ parts })" in document
    assert "function ToolActivityRow({ part })" in document
    assert "function toolActivityView(part)" in document
    assert "if (part.type === 'provider_state')" in document
    assert "const prefix = running ? '正在' : '已完成'" in document
    assert "检查项目上下文" in document
    assert "更新项目文件" in document
    assert "运行代码质量检查" in document
    assert "查看工作说明" in document
    assert "原始调用 · ${view.tool}" in document
    assert "执行结果" in document


def test_chat_run_progress_combines_plan_and_workspace_evidence() -> None:
    document = INDEX.read_text(encoding="utf-8")
    styles = PARTS_STYLES.read_text(encoding="utf-8")

    assert "function RunProgressDock({ runId, status, todos, runParts })" in document
    assert "'/runs/' + encodeURIComponent(runId) + '/workspace'" in document
    assert "第 ${step} / ${items.length} 步" in document
    assert "本轮修改 ${runFiles.length} 个文件" in document
    assert "可能包含本轮开始前的改动" in document
    assert "['write', 'edit', 'apply_patch'].includes(part.tool)" in document
    assert "const isActive = status === 'busy' || hasRunningTool" in document
    assert "const effectiveSessionStatus = useMemo(() =>" in document
    assert "return hasRunningTool ? 'busy' : sessionStatus" in document
    assert '.run-progress-pill' in styles
    assert '.run-progress-popover' in styles
    assert '.activity-row-details' in styles


def test_permission_decision_has_immediate_feedback_and_is_single_submit() -> None:
    document = INDEX.read_text(encoding="utf-8")
    styles = PARTS_STYLES.read_text(encoding="utf-8")

    assert "if (submission) return" in document
    assert "setSubmission({ status: 'submitting', reply: reply })" in document
    assert "Decision received. Resuming agent…" in document
    assert 'role="status" aria-live="polite"' in document
    assert "throw e;" in document
    assert ".permission-card.replying" in styles
    assert "@keyframes permission-spin" in styles
