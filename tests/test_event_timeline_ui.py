from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "nexapilot" / "web" / "index.html"
STYLES = ROOT / "src" / "nexapilot" / "web" / "styles" / "observability.css"


def test_event_panel_projects_runtime_events_into_a_state_flow() -> None:
    document = INDEX.read_text(encoding="utf-8")

    assert "CURRENT STATE" in document
    assert "Preparing model call" in document
    assert "Waiting for model" in document
    assert "Executing tool" in document
    assert "Waiting for approval" in document
    assert "User input stored" in document
    assert "messageRoles.get(part.message_id)" in document
    assert "Run ${copy[0].toLowerCase()}" in document
    assert "scope=tree" in document
    assert "Subagent started" in document
    assert "Tool budget reached" in document
    assert "All agents" in document
    assert "Subagents" in document
    assert "isActiveSessionEvent" in document

    for state in (
        "queued",
        "acquiring",
        "running",
        "waiting_approval",
        "waiting_retry",
        "recovery_pending",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    ):
        assert f"{state}:" in document


def test_event_panel_coalesces_stream_updates_and_preserves_raw_evidence() -> None:
    document = INDEX.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert "const eventKey = objectId" in document
    assert "prev.filter(item => item.key !== eventKey)" in document
    assert "Current run" in document
    assert "latestRunId" in document
    assert "Details" in document
    assert ".reverse()" not in document[document.index("const visible = scoped"):document.index("return html`", document.index("const visible = scoped"))]
    assert "Technical event · ${evt.type}" in document
    assert "JSON.stringify(evt.properties || {}, null, 2)" in document
    assert ".events-timeline::before" in styles
    assert ".event-current.running .event-current-icon" in styles
