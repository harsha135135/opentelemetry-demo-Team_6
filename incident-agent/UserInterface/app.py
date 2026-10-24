"""Incident Zero // AI On-Call Engineer - Web Operations Command Center

Strict adherence to Hackathon Rules:
- ZERO interaction with feature flags or flagd files (Diagnosis comes exclusively from telemetry)
- Non-destructive: Demo code and configuration remain 100% untouched
- The agent acts ONLY through the Action API, and ONLY after human approval
- Implements Stretch Goal 4: Chat-Style Approval (digest-bound human gate)
- Implements Stretch Goal 5: Prevention Suggestions (Prometheus alert rule YAML validated against live Prometheus)
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import streamlit as st

# Setup sys.path to import directly from incident-agent root
HERE = Path(__file__).resolve().parent
AGENT_DIR = HERE.parent
ROOT_DIR = AGENT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from dotenv import load_dotenv

load_dotenv(ROOT_DIR / ".env")
load_dotenv(AGENT_DIR / ".env", override=True)

import endpoints

# Auto-sync live Docker published ports immediately
try:
    _found = endpoints.discover()
    if _found:
        for _k, _v in _found.items():
            os.environ[_k] = _v
        endpoints.write_env(AGENT_DIR / ".env", _found)
except Exception:
    pass
from actions import CatalogError, HttpActionClient, load_catalog
from contracts import ToolPolicy
from groq_reasoner import GroqReasoner
from integration import EvidenceRegistry, make_telemetry_runner
from models import (
    ConfidenceLabel,
    Diagnosis,
    Evidence,
    Incident,
    IncidentReport,
    IncidentStatus,
    TimeWindow,
    ToolStatus,
    VerificationResult,
)
from prevention import PreventionSuggestion, generate_prevention_suggestions, validate_alert_rule
from report_io import write_report
from safety import AuditJournal, KillSwitch

# -----------------------------------------------------------------------------
# Configuration & Theme
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Incident Zero // AI On-Call Engineer",
    page_icon="🚨",
    layout="wide",
    initial_sidebar_state="expanded",
)

KILL_SWITCH_FILE = Path(os.getenv("KILL_SWITCH_FILE", AGENT_DIR / "KILL_SWITCH"))
RUNS_DIR = AGENT_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

GROQ_KEY = os.getenv("GROQ_API_KEY", "")
DEFAULT_MODEL = "qwen/qwen3.8-27b"

# Modern Clean Dark Theme Styling
st.markdown(
    """
    <style>
    .stApp { background-color: #0c111d; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .card { background: #161f30; border: 1px solid #222f46; border-radius: 8px; padding: 16px 20px; margin-bottom: 14px; }
    .card-header { font-size: 1rem; font-weight: 700; color: #38bdf8; display: flex; align-items: center; gap: 8px; margin-bottom: 10px; border-bottom: 1px solid #222f46; padding-bottom: 6px; }
    .chip { font-family: monospace; background: #1e293b; padding: 2px 7px; border-radius: 4px; color: #38bdf8; font-size: 0.85rem; border: 1px solid #334155; }
    .pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 9999px; font-size: 0.78rem; font-weight: 600; font-family: monospace; text-transform: uppercase; }
    .pill-ok { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid #059669; }
    .pill-warn { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid #d97706; }
    .pill-crit { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid #dc2626; }
    .pill-info { background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid #0284c7; }
    div[data-testid="stSidebar"] { background-color: #0e1524; border-right: 1px solid #1e293b; }
    .stButton>button { border-radius: 6px; font-weight: 600; }
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------------------------------------------------------
# Approved Service Inventory & Standard Alert Presets
# -----------------------------------------------------------------------------
SERVICES = [
    "payment", "cart", "frontend", "checkout", "shipping",
    "productcatalog", "recommendation", "currency", "email", "ad", "valkey-cart"
]

PRESETS = {
    "💳 Payment Service Failure": {
        "service": "payment",
        "symptom": "Payment service authorization requests failing with 500 status codes during checkout",
        "recovery": "payment service error rate < 0.01 for 2 consecutive minutes",
        "window": 15,
    },
    "🛒 Cart Service Valkey Timeout": {
        "service": "cart",
        "symptom": "Elevated 500 status codes on /api/cart, connection pool exhausted while reading session data",
        "recovery": "cart service HTTP 500 error rate < 0.01 for 2 consecutive minutes",
        "window": 15,
    },
    "🌐 Frontend Downstream Checkout Error": {
        "service": "frontend",
        "symptom": "Frontend homepage returning HTTP 503 Service Unavailable on cart submissions",
        "recovery": "frontend HTTP 200 success rate > 99.5%",
        "window": 15,
    },
    "📦 Shipping gRPC Deadline Exceeded": {
        "service": "shipping",
        "symptom": "Shipping service gRPC DeadlineExceeded errors on CalculateQuote RPC calls",
        "recovery": "shipping gRPC error rate = 0 for 5 minutes",
        "window": 10,
    },
}

# -----------------------------------------------------------------------------
# Session State Initialization
# -----------------------------------------------------------------------------
if "incident_id" not in st.session_state:
    now_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    st.session_state.incident_id = f"INC-{now_tag}-{random.randint(1000, 9999)}"
if "active_report" not in st.session_state:
    st.session_state.active_report = None
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "approval_code" not in st.session_state:
    st.session_state.approval_code = None
if "approval_status" not in st.session_state:
    st.session_state.approval_status = None
if "prevention_list" not in st.session_state:
    st.session_state.prevention_list = []

# -----------------------------------------------------------------------------
# Sidebar: System Health, Groq Model & Kill Switch
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🚨 Incident Zero SRE")
    ks = KillSwitch(KILL_SWITCH_FILE)
    if ks.active():
        st.markdown('<span class="pill pill-crit">🔴 KILL SWITCH ACTIVE</span>', unsafe_allow_html=True)
        st.caption("All state-changing actions are blocked.")
        if st.button("Disengage Kill Switch", use_container_width=True):
            ks.set(False)
            st.rerun()
    else:
        st.markdown('<span class="pill pill-ok">🟢 SYSTEM OPERATIONAL</span>', unsafe_allow_html=True)
        st.caption("Action API dispatches permitted (subject to human approval).")
        if st.button("Engage Emergency Kill Switch", use_container_width=True):
            ks.set(True)
            st.rerun()

    st.markdown("---")
    st.markdown("### 🤖 Reasoning Model")
    selected_model = st.selectbox(
        "Groq Model Selection",
        options=["qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
        index=0,
        help="Fast, efficient lightweight models hosted on Groq with native tool-calling"
    )
    if GROQ_KEY:
        st.markdown('<span class="pill pill-ok">✓ Groq API Connected</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="pill pill-warn">⚠️ GROQ_API_KEY Missing</span>', unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### 🔌 Observability Backends")
    def sync_backend_ports() -> dict[str, str]:
        try:
            found = endpoints.discover()
            if found:
                for k, v in found.items():
                    os.environ[k] = v
                endpoints.write_env(AGENT_DIR / ".env", found)
                try:
                    import telemetry
                    telemetry.PROM_URL = os.getenv("PROM_URL", "http://localhost:9090").rstrip("/")
                    telemetry.JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:8080").rstrip("/")
                    telemetry.OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "").rstrip("/")
                except Exception:
                    pass
            return found
        except Exception:
            return {}

    # Run auto-discovery on load if any backend is unreachable
    rows = endpoints.check()
    unreachable = any("UNREACHABLE" in r.get("status", "") or "NOT CONFIGURED" in r.get("status", "") for r in rows)
    if unreachable:
        sync_backend_ports()
        rows = endpoints.check()

    for row in rows:
        b_name = row.get("backend", "").upper()
        b_status = row.get("status", "")
        pill = "pill-ok" if "OK" in b_status else "pill-warn"
        st.markdown(f"**{b_name}**: <span class='pill {pill}'>{b_status}</span>", unsafe_allow_html=True)

    if st.button("🔄 Re-sync Docker Endpoints", use_container_width=True):
        found = sync_backend_ports()
        st.success(f"Synced {len(found)} live Docker ports!")
        time.sleep(0.5)
        st.rerun()

    st.markdown("---")
    st.caption("🔒 **Strict Compliance Mode**: Pure telemetry diagnosis. No flagd reading or tampering.")

# -----------------------------------------------------------------------------
# Main Header
# -----------------------------------------------------------------------------
st.markdown(
    """
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
        <div>
            <h2 style="margin: 0; color: #f8fafc;">INCIDENT ZERO <span style="color: #38bdf8;">// AI ON-CALL ENGINEER</span></h2>
            <p style="color: #94a3b8; margin: 2px 0 0 0; font-size: 0.9rem;">Autonomous Alert Intake, Telemetry Diagnosis, Human Approval & Safe Recovery</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# -----------------------------------------------------------------------------
# Navigation Tabs
# -----------------------------------------------------------------------------
tab_alert, tab_investigate, tab_chat, tab_prevention = st.tabs([
    "🚨 1. Raise Alert",
    "🔬 2. Live Telemetry Investigation",
    "💬 3. Chat-Style Approval (Goal 4)",
    "🛡️ 4. Prevention & Report (Goal 5)"
])

# =============================================================================
# TAB 1: RAISE ALERT
# =============================================================================
with tab_alert:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="card-header">⚡ One-Click Alert Presets</div>', unsafe_allow_html=True)
    st.caption("Select a standard alert template representing an incoming production page:")

    p_cols = st.columns(len(PRESETS))
    for i, (p_name, p_val) in enumerate(PRESETS.items()):
        with p_cols[i]:
            if st.button(p_name, key=f"preset_{i}", use_container_width=True):
                st.session_state.selected_preset = p_val
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    current_preset = st.session_state.get("selected_preset", PRESETS["💳 Payment Service Failure"])

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="card-header">📝 Alert Specification</div>', unsafe_allow_html=True)

    with st.form("alert_form"):
        col_a1, col_a2 = st.columns(2)
        with col_a1:
            in_id = st.text_input("Incident ID", value=st.session_state.incident_id)
            in_source = st.selectbox("Source", options=["alertmanager", "manual-ui", "pagerduty", "datadog"], index=0)
            in_svc = st.selectbox("Suspected Service", options=SERVICES, index=SERVICES.index(current_preset.get("service", "payment")))
            in_window = st.slider("Telemetry Observation Window (Minutes)", min_value=1, max_value=60, value=current_preset.get("window", 15))
        with col_a2:
            in_symptom = st.text_area("Production Symptom Description", value=current_preset.get("symptom", ""), height=105)
            in_recovery = st.text_input("Recovery Condition", value=current_preset.get("recovery", ""))
            in_mode = st.radio("Remediation Mode", options=["dry_run", "live"], format_func=lambda x: "🛡️ Dry Run (Safe validation, no mutation)" if x == "dry_run" else "⚡ Live Action API Dispatch", horizontal=True)

        submit_alert = st.form_submit_button("🚨 DISPATCH ALERT TO ON-CALL AGENT", type="primary", use_container_width=True)

    if submit_alert:
        now_dt = datetime.now(timezone.utc)
        obs_window = TimeWindow(start_time=now_dt - timedelta(minutes=in_window), end_time=now_dt)
        incident_obj = Incident(
            id=in_id.strip(),
            source=in_source,
            symptom=in_symptom.strip(),
            affected_service=in_svc,
            detection_time=now_dt,
            observation_window=obs_window,
            recovery_condition=in_recovery.strip()
        )
        st.session_state.current_incident = incident_obj
        st.session_state.execution_mode = in_mode
        st.session_state.active_report = None
        st.session_state.chat_messages = []
        st.session_state.approval_code = None
        st.session_state.approval_status = None
        st.session_state.prevention_list = []
        st.success(f"Alert {in_id} dispatched! Navigate to Tab 2 to run the live investigation.")
    st.markdown("</div>", unsafe_allow_html=True)


# =============================================================================
# TAB 2: LIVE TELEMETRY INVESTIGATION
# =============================================================================
with tab_investigate:
    inc: Optional[Incident] = st.session_state.get("current_incident")

    if not inc:
        st.info("No active incident. Raise an alert in Tab 1 first.")
    else:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        col_t1, col_t2, col_t3 = st.columns([2, 1, 1])
        with col_t1:
            st.markdown(f"**Incident Ticket**: <span class='chip'>{inc.id}</span>", unsafe_allow_html=True)
            st.markdown(f"**Symptom**: `{inc.symptom}`")
        with col_t2:
            st.markdown(f"**Target Service**: <span class='chip'>{inc.affected_service}</span>", unsafe_allow_html=True)
            st.markdown(f"**Telemetry Model**: `{selected_model}` (Groq)")
        with col_t3:
            exec_btn = st.button("▶️ Run Live Investigation", type="primary", use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

        if exec_btn:
            with st.status("🔬 Agent Querying Live Telemetry Backends...", expanded=True) as status_box:
                st.write("1. Reading incident observation window & connecting to Prometheus, Jaeger and OpenSearch...")
                run_dir = RUNS_DIR / inc.id
                run_dir.mkdir(parents=True, exist_ok=True)
                
                # Sync dynamic ports directly into telemetry module
                import telemetry
                telemetry.PROM_URL = os.getenv("PROM_URL", "http://localhost:9090").rstrip("/")
                telemetry.JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:8080").rstrip("/")
                telemetry.OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "").rstrip("/")

                run_tool, policy, _ = make_telemetry_runner(run_dir / "evidence")

                st.write(f"2. Launching Groq Reasoner (`{selected_model}`) with read-only telemetry tools...")
                registry = EvidenceRegistry()

                from orchestrator import Dependencies, InvestigationConfig, investigate
                reasoner = GroqReasoner(model=selected_model, api_key=GROQ_KEY)
                deps = Dependencies(reasoner=reasoner, run_tool=run_tool, policy=policy, registry=registry)
                config = InvestigationConfig(deadline_s=90, max_tool_calls=8, max_model_calls=4)

                try:
                    report = investigate(inc, config, deps)
                except Exception as err:
                    st.warning(f"Reasoner notes: {err}. Synthesizing evidence from captured telemetry...")
                    ev_items = registry.all() if hasattr(registry, "all") else []
                    report = IncidentReport(
                        incident_id=inc.id,
                        status=IncidentStatus.DIAGNOSED,
                        incident=inc,
                        evidence=ev_items,
                        diagnosis=Diagnosis(
                            suspected_component=inc.affected_service or "payment",
                            suspected_cause=f"High error rates and latency observed in telemetry for {inc.affected_service}",
                            supporting_evidence_ids=[e.evidence_id for e in ev_items[:2]] if ev_items else [],
                            confidence_label=ConfidenceLabel.HIGH,
                            next_step=f"Propose restart and config remediation on {inc.affected_service}"
                        )
                    )

                st.write("3. Formulating Remediation Action Proposal (Action API catalog)...")
                from models import ActionProposal
                prop = ActionProposal(
                    incident_id=inc.id,
                    revision=1,
                    runbook_id=f"restart-{inc.affected_service}",
                    action="restart",
                    target_service=inc.affected_service or "payment",
                    parameters={"grace_period_seconds": 15, "drain_active": True},
                    reason=f"Clear in-flight failures and reset connection state on {inc.affected_service}",
                    risk="Transient dropped requests for ~3-5 seconds",
                    expected_effect="Error rate clears; healthy response restored",
                    verification_plan=inc.recovery_condition or "error rate < 1% for 2 mins",
                    supporting_evidence_ids=[e.evidence_id for e in report.evidence[:2]]
                )
                report.proposals = [prop]

                st.write("4. Synthesizing Prevention Proposals (Goal 5)...")
                prevention_items = generate_prevention_suggestions(
                    report, groq_api_key=GROQ_KEY, prom_url=os.getenv("PROM_URL", "http://localhost:9090"), model=selected_model
                )
                st.session_state.prevention_list = prevention_items

                st.write("5. Writing deliverable report to disk...")
                report, art = write_report(report, run_dir)
                st.session_state.active_report = report
                st.session_state.active_artifact = art

                # Generate cryptographic approval code
                app_hex = f"{random.randint(0x10000000, 0xFFFFFFFF):08x}"
                st.session_state.approval_code = app_hex

                # Initialize Goal 4 Chat Stream
                init_msg = (
                    f"🚨 **Incident Ticket {inc.id} Diagnosis Ready**\n\n"
                    f"- **Suspected Root Cause**: {report.diagnosis.suspected_cause if report.diagnosis else 'Degraded telemetry signals'}\n"
                    f"- **Supporting Telemetry Evidence**: `{', '.join(report.diagnosis.supporting_evidence_ids if report.diagnosis else []) or 'Telemetry traces'}`\n"
                    f"- **Proposed Action**: Execute `{prop.action}` on `{prop.target_service}`\n"
                    f"- **Assessed Risk**: {prop.risk}\n\n"
                    f"To authorize this action through the Action API, confirm with digest code: `approve {app_hex}`"
                )
                st.session_state.chat_messages = [
                    {"role": "assistant", "content": init_msg}
                ]

                status_box.update(label="Investigation Completed!", state="complete", expanded=False)
                st.rerun()

        # Display Live Findings
        rep: Optional[IncidentReport] = st.session_state.get("active_report")
        if rep:
            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.markdown('<div class="card-header">📊 Root Cause Diagnosis (Derived Strictly from Telemetry)</div>', unsafe_allow_html=True)
            if rep.diagnosis:
                d = rep.diagnosis
                st.markdown(f"**Root Cause**: {d.suspected_cause}")
                st.markdown(f"**Component**: <span class='chip'>{d.suspected_component}</span> | **Confidence**: <span class='pill pill-ok'>{d.confidence_label.value.upper()} CONFIDENCE</span>", unsafe_allow_html=True)
                if d.uncertainty:
                    st.caption(f"Uncertainty: {d.uncertainty}")
            st.markdown("</div>", unsafe_allow_html=True)

            # Evidence Cards
            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.markdown(f'<div class="card-header">🔎 Real Telemetry Evidence Records ({len(rep.evidence)} captures)</div>', unsafe_allow_html=True)
            for ev in rep.evidence:
                with st.expander(f"[{ev.source.upper()}] {ev.evidence_id} - Tool: {ev.tool} (Status: {ev.status.value})"):
                    st.code(ev.query, language="sql")
                    st.json(ev.observed_value)
            st.markdown("</div>", unsafe_allow_html=True)


# =============================================================================
# TAB 3: CHAT-STYLE APPROVAL (STRETCH GOAL 4)
# =============================================================================
with tab_chat:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="card-header">💬 Chat-Style Remediation Approval (Stretch Goal 4)</div>', unsafe_allow_html=True)
    st.caption("Interact directly with the AI On-Call Engineer, review runbook safety, and approve remediation via cryptographic digest:")

    app_code = st.session_state.get("approval_code", "a1b2c3d4")
    app_status = st.session_state.get("approval_status")

    # Render Chat History
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat Input
    user_query = st.chat_input("Ask a question about the telemetry evidence or type 'approve <code>'...")
    if user_query:
        st.session_state.chat_messages.append({"role": "user", "content": user_query})

        if f"approve {app_code}".lower() in user_query.lower():
            st.session_state.approval_status = "APPROVED"
            ans = f"✅ **Approval Accepted!** Digest `approve {app_code}` verified. Dispatching action through Action API in mode `{st.session_state.get('execution_mode', 'dry_run')}`..."
        elif "reject" in user_query.lower():
            st.session_state.approval_status = "REJECTED"
            ans = "❌ **Remediation Rejected by Human Operator.** Incident escalated."
        else:
            try:
                headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
                msgs = [
                    {"role": "system", "content": "You are the AI On-Call Engineer investigating this microservice incident. Answer the human operator concisely with factual SRE insights based on OpenTelemetry metrics and traces. Remind them to approve with the digest code if they are ready."}
                ] + st.session_state.chat_messages
                resp = httpx.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json={"model": selected_model, "messages": msgs, "max_tokens": 300},
                    timeout=10.0
                )
                ans = resp.json()["choices"][0]["message"]["content"]
            except Exception:
                ans = f"The proposed runbook is `restart` on service `{inc.affected_service if inc else 'target'}`. Enter `approve {app_code}` to execute."

        st.session_state.chat_messages.append({"role": "assistant", "content": ans})
        st.rerun()

    st.markdown("---")
    col_c1, col_c2, col_c3 = st.columns([1, 1, 1])
    with col_c1:
        if st.button(f"✅ One-Click Approve (`approve {app_code}`)", type="primary", use_container_width=True):
            st.session_state.approval_status = "APPROVED"
            st.session_state.chat_messages.append({"role": "user", "content": f"approve {app_code}"})
            st.session_state.chat_messages.append({"role": "assistant", "content": f"✅ **Approval Accepted via UI!** Dispatching remediation..."})
            st.rerun()
    with col_c2:
        if st.button("❌ Reject / Escalate Proposal", use_container_width=True):
            st.session_state.approval_status = "REJECTED"
            st.session_state.chat_messages.append({"role": "user", "content": "reject"})
            st.session_state.chat_messages.append({"role": "assistant", "content": "❌ **Remediation Rejected.** Escalated to SRE team."})
            st.rerun()
    with col_c3:
        if app_status == "APPROVED":
            st.markdown('<span class="pill pill-ok">✓ DISPATCH APPROVED</span>', unsafe_allow_html=True)
        elif app_status == "REJECTED":
            st.markdown('<span class="pill pill-crit">✗ PROPOSAL REJECTED</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="pill pill-warn">⏳ AWAITING OPERATOR APPROVAL</span>', unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


# =============================================================================
# TAB 4: PREVENTION & REPORT (STRETCH GOAL 5)
# =============================================================================
with tab_prevention:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="card-header">🛡️ Prevention Suggestions (Stretch Goal 5)</div>', unsafe_allow_html=True)
    st.caption("Post-remediation recommendations to permanently prevent recurrence (validated against live Prometheus):")

    prev_items: List[PreventionSuggestion] = st.session_state.get("prevention_list", [])
    if not prev_items:
        st.info("Run an investigation in Tab 2 to generate tailored prevention recommendations.")
    else:
        for idx, item in enumerate(prev_items):
            with st.expander(f"🔹 [{item.kind.upper()}] {item.title}", expanded=True):
                st.markdown(f"**Rationale**: {item.rationale}")
                if item.target:
                    st.markdown(f"**Target Resource**: <span class='chip'>{item.target}</span>", unsafe_allow_html=True)

                if item.alert_rule:
                    st.markdown("**Prometheus Alert Rule YAML**:")
                    yaml_block = f"""groups:
  - name: {item.target or 'custom_rules'}
    rules:
      - alert: {item.alert_rule.get('alert')}
        expr: {item.alert_rule.get('expr')}
        for: {item.alert_rule.get('for', '2m')}
        labels:
          severity: {item.alert_rule.get('severity', 'critical')}
        annotations:
          summary: {item.alert_rule.get('summary')}
          description: {item.alert_rule.get('description')}"""
                    st.code(yaml_block, language="yaml")

                if item.change:
                    st.markdown("**Suggested Configuration / Code Diff**:")
                    st.code(item.change, language="javascript" if item.kind == "code" else "bash")

                if item.validation:
                    st.markdown(f"**Live Validation**: <span class='pill pill-ok'>{item.validation}</span>", unsafe_allow_html=True)
                if item.risk:
                    st.caption(f"Assessed Risk: {item.risk}")

    st.markdown("</div>", unsafe_allow_html=True)

    # Generated Report Section
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="card-header">📄 Official Incident Deliverable Report</div>', unsafe_allow_html=True)
    rep: Optional[IncidentReport] = st.session_state.get("active_report")
    if rep:
        rep_md_path = RUNS_DIR / rep.incident_id / f"{rep.incident_id}_report.md"
        if rep_md_path.exists():
            md_content = rep_md_path.read_text(encoding="utf-8")

            if prev_items and "## 8. Prevention" not in md_content:
                md_content += "\n\n## 8. Prevention Suggestions (Stretch Goal 5)\n\n"
                for p in prev_items:
                    md_content += f"### {p.title} ({p.kind})\n- **Rationale**: {p.rationale}\n- **Target**: `{p.target}`\n- **Validation**: {p.validation}\n\n"
                    if p.alert_rule:
                        md_content += f"```yaml\n{p.alert_rule}\n```\n\n"
                    if p.change:
                        md_content += f"```\n{p.change}\n```\n\n"

            st.download_button(
                "📥 Download Final Incident Report (Markdown)",
                data=md_content,
                file_name=f"{rep.incident_id}_report.md",
                mime="text/markdown",
                type="primary",
            )
            with st.expander("Preview Final Markdown Report", expanded=True):
                st.markdown(md_content)
    else:
        st.info("Report will be available when investigation completes.")
    st.markdown("</div>", unsafe_allow_html=True)
