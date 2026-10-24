"""Team Action API server (standard library HTTP + Pydantic models). Run with `python cli.py action-api serve`.

Security model (see docs/ACTION_API_ARCHITECTURE.md):
- bearer token on every /v1 endpoint; binds to 127.0.0.1 by default; JSON only; 16 KB body limit
- strict allowlist of operations and targets; arguments validated against the operation schema
- approval is created by the server, bound to the SHA-256 digest of the exact payload, and unlocked by a one-time
  code printed only on this server's console (stored as a hash, never returned, 5 attempts, TTL <= 300s, single use)
- the human enters that code either at the agent prompt or on the browser page /ui/approvals/<id>; the page has no
  bearer token and no approve endpoint exists under /v1, so the agent cannot approve its own request
- one action per incident, idempotency keys, kill switch checked at execute and again in the worker
- state persisted atomically; records left running by a crash become 'unknown' and are never resumed
- append-only audit log; tokens and approval codes are never logged
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import re
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs

from pydantic import BaseModel, ValidationError

from action_api_models import (
    API_VERSION, printable, TERMINAL_STATUSES, ActionList, ActionPayload, ActionRecord, ActionResultResponse, Approval,
    ApprovalCreateRequest, ApprovalRejectRequest, AuditInfo, ErrorCode, ErrorDetail, ErrorResponse, ExecuteRequest,
    ExecutionResult, Health, LoadGeneratorState, OperationList, OperationSpec, ServiceState, SystemState,
    ValidateResponse, VerificationReport,
)
import approval_ui
from actions import Operation, validate_arguments

MAX_BODY = 16 * 1024
MAX_CODE_ATTEMPTS = 5
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MAX_FORM_BODY = 4096
_APPROVAL_PATH_ID = re.compile(r"^apr-[a-f0-9]{16}$")  # approval ids as issued by create_approval
_FORM_CODE = re.compile(r"^[A-Z2-9]{4}-?[A-Z2-9]{4}$")  # the one-time code, with or without its dash


class ApiError(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str, details: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or []

    def body(self) -> dict:
        return ErrorResponse(error=ErrorDetail(code=self.code, message=self.message, details=self.details)).model_dump(mode="json")


def restart_operation(targets: list[str]) -> Operation:
    return Operation(
        name="restart_service", description="Restart one allowlisted application or worker container (docker restart).",
        method="POST", path="/v1/actions/execute", target_kind="service", target_field="target",
        allowed_targets=targets,
        parameters={"grace_period_seconds": {"type": "integer", "minimum": 0, "maximum": 30, "default": 10,
                                             "description": "seconds to wait for graceful stop"}},
        risk="In-flight requests to the service fail while it restarts (typically 5-20s). Does not fix bad code, "
             "configuration, dependencies or network faults.",
        reversible=False, idempotent=False,
        expected_effect="The container restarts and resumes reporting; faults caused by a crashed, stopped or hung "
                        "process clear.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _code_hash(approval_id: str, code: str) -> str:
    return hashlib.sha256(f"{approval_id}:{code.replace('-', '').upper()}".encode()).hexdigest()


class ActionApiService:
    def __init__(self, *, executor: Any, targets: list[str], state_dir: Path, kill_switch: Callable[[], bool],
                 announce: Callable[[str], None] = lambda s: print(s, file=sys.stderr, flush=True),
                 clock: Callable[[], datetime] = _now, run_async: bool = True,
                 inspector: Optional[Callable[[list[str]], list[dict]]] = None, state_services: Optional[list[str]] = None,
                 loadgen_url: str = "", ui_base: str = "", grafana_url: str = "") -> None:
        self.executor, self.kill_switch, self.announce, self.clock = executor, kill_switch, announce, clock
        self.ui_base, self.grafana_url = ui_base.rstrip("/"), grafana_url.rstrip("/")
        self.inspector, self.loadgen_url = inspector, loadgen_url.rstrip("/")
        self.state_services = state_services or []
        self.operations = {"restart_service": restart_operation(targets)}
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path, self.audit_path = self.state_dir / "state.json", self.state_dir / "audit.jsonl"
        self.lock = threading.RLock()
        self.run_async = run_async
        self.approvals: dict[str, dict] = {}
        self.actions: dict[str, ActionRecord] = {}
        self._load()

    # ---- persistence and audit
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.approvals = data.get("approvals", {})
        for raw in data.get("actions", []):
            rec = ActionRecord.model_validate(raw)
            if rec.status not in TERMINAL_STATUSES:
                rec = rec.model_copy(update={"status": "unknown", "finished_at": self.clock()})
                self._audit("action_marked_unknown_after_restart", action_id=rec.action_id, incident_id=rec.incident_id)
            self.actions[rec.action_id] = rec
        self._save()

    def _save(self) -> None:
        data = {"approvals": self.approvals, "actions": [a.model_dump(mode="json") for a in self.actions.values()]}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _audit(self, event: str, **fields: Any) -> None:
        rec = {"time": self.clock().isoformat(), "event": event, **fields}
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # ---- validation
    def _check_payload(self, payload: ActionPayload) -> dict:
        op = self.operations.get(payload.operation)
        if op is None:
            raise ApiError(400, ErrorCode.UNKNOWN_OPERATION, f"operation '{payload.operation}' is not allowlisted",
                           [f"allowed: {', '.join(self.operations)}"])
        if payload.target not in op.allowed_targets:
            raise ApiError(403, ErrorCode.TARGET_NOT_ALLOWED, f"target '{payload.target}' is not allowlisted for {op.name}")
        args, errors = validate_arguments(op, dict(payload.arguments))
        if errors:
            raise ApiError(422, ErrorCode.INVALID_ARGUMENTS, "arguments do not match the operation schema", errors)
        return args

    def _normalized(self, payload: ActionPayload) -> ActionPayload:
        return payload.model_copy(update={"arguments": self._check_payload(payload)})

    # ---- read endpoints
    def health(self) -> Health:
        return Health(api_version=API_VERSION, executor=self.executor.mode, kill_switch_active=self.kill_switch(), time=self.clock())

    def list_operations(self) -> OperationList:
        ops = [OperationSpec(name=o.name, description=o.description, allowed_targets=o.allowed_targets,
                             parameters=o.parameters, required=o.required, risk=o.risk, reversible=o.reversible,
                             idempotent=o.idempotent, expected_effect=o.expected_effect) for o in self.operations.values()]
        return OperationList(api_version=API_VERSION, executor=self.executor.mode, operations=ops)

    def system_state(self) -> SystemState:
        targets = set(self.operations["restart_service"].allowed_targets)
        if self.inspector is None:
            services, source = [], "unavailable: no docker CLI for read-only inspection"
        else:
            try:
                services = [ServiceState(**r, restartable=r["service"] in targets) for r in self.inspector(self.state_services)]
                source = "docker inspect (read-only)"
            except Exception as exc:  # noqa: BLE001
                services, source = [], f"unavailable: {type(exc).__name__}"
        return SystemState(time=self.clock(), source=source, services=services, load_generator=self._loadgen(),
                           kill_switch_active=self.kill_switch())

    def _loadgen(self) -> Optional[LoadGeneratorState]:
        if not self.loadgen_url:
            return None
        import httpx
        try:
            d = httpx.get(f"{self.loadgen_url}/stats/requests", timeout=3, follow_redirects=False).json()
            return LoadGeneratorState(available=True, state=str(d.get("state")), user_count=d.get("user_count"),
                                      total_rps=round(float(d.get("total_rps") or 0), 3),
                                      fail_ratio=round(float(d.get("fail_ratio") or 0), 4))
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return LoadGeneratorState(available=False, detail=f"load generator stats unavailable: {type(exc).__name__}")

    def validate(self, payload: ActionPayload) -> ValidateResponse:
        try:
            p = self._normalized(payload)
        except ApiError as e:
            return ValidateResponse(valid=False, errors=[ErrorDetail(code=e.code, message=e.message, details=e.details)])
        self._audit("validated", incident_id=p.incident_id, operation=p.operation, target=p.target, payload_digest=p.digest())
        return ValidateResponse(valid=True, payload_digest=p.digest(), normalized_arguments=p.arguments)

    def _approval_view(self, a: dict) -> Approval:
        if a["status"] in ("pending", "approved") and self.clock() > datetime.fromisoformat(a["expires_at"]):
            a["status"] = "expired"
            self._save()
        return Approval(**{k: v for k, v in a.items() if k in Approval.model_fields})

    def get_approval(self, approval_id: str) -> Approval:
        with self.lock:
            a = self.approvals.get(approval_id)
            if a is None:
                raise ApiError(404, ErrorCode.APPROVAL_NOT_FOUND, "no such approval")
            return self._approval_view(a)

    def get_action(self, action_id: str) -> ActionRecord:
        rec = self.actions.get(action_id)
        if rec is None:
            raise ApiError(404, ErrorCode.NOT_FOUND, "no such action")
        return rec

    def result(self, action_id: str) -> ActionResultResponse:
        r = self.get_action(action_id)
        return ActionResultResponse(action_id=r.action_id, status=r.status, terminal=r.status in TERMINAL_STATUSES,
                                    result=r.result, finished_at=r.finished_at)

    def list_actions(self, limit: int = 50) -> ActionList:
        return ActionList(actions=sorted(self.actions.values(), key=lambda a: a.created_at, reverse=True)[:limit])

    # ---- approvals
    def create_approval(self, req: ApprovalCreateRequest) -> Approval:
        with self.lock:
            payload = self._normalized(req.payload)
            approval_id = f"apr-{uuid.uuid4().hex[:16]}"
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
            now = self.clock()
            a = {"approval_id": approval_id, "status": "pending", "incident_id": payload.incident_id,
                 "operation": payload.operation, "target": payload.target, "payload_digest": payload.digest(),
                 "requested_by": req.requested_by, "created_at": now.isoformat(),
                 "expires_at": (now + timedelta(seconds=req.ttl_seconds)).isoformat(), "failed_attempts": 0,
                 "code_hash": _code_hash(approval_id, code), "payload": payload.model_dump(mode="json"),
                 "form_token": secrets.token_urlsafe(18), "approval_page": f"/ui/approvals/{approval_id}"}
            self.approvals[approval_id] = a
            self._save()
            self._audit("approval_created", approval_id=approval_id, incident_id=payload.incident_id,
                        operation=payload.operation, target=payload.target, arguments=payload.arguments,
                        payload_digest=a["payload_digest"], requested_by=req.requested_by, expires_at=a["expires_at"])
            # Free text first (escaped); the authoritative action block is printed last so nothing can redraw it.
            self.announce("\n".join([
                "=" * 70, f"APPROVAL REQUEST {approval_id}  incident {payload.incident_id}",
                f"  reason (from the model, untrusted): {printable(payload.reason, 300)}",
                f"  requested by: {printable(req.requested_by, 80)}",
                f"  evidence: {', '.join(payload.evidence_ids)}",
                "  ---- exact action (authoritative) ----",
                f"  ACTION:   {payload.operation}", f"  TARGET:   {payload.target}", f"  ARGS:     {payload.arguments!r}",
                f"  DIGEST:   {a['payload_digest'][:16]}...  expires {a['expires_at']}",
                f"  ONE-TIME APPROVAL CODE: {code[:4]}-{code[4:]}",
                f"  APPROVAL PAGE: {self.ui_base}/ui/approvals/{approval_id}",
                "  Enter this code on that page (or at the agent prompt) only if you approve this exact action.",
                "=" * 70]))
            return self._approval_view(a)

    def reject_approval(self, approval_id: str, req: ApprovalRejectRequest) -> Approval:
        with self.lock:
            a = self.approvals.get(approval_id)
            if a is None:
                raise ApiError(404, ErrorCode.APPROVAL_NOT_FOUND, "no such approval")
            if a["status"] == "pending":
                a["status"] = "rejected"
                self._save()
            self._audit("approval_rejected", approval_id=approval_id, by=req.rejected_by, reason=req.reason[:200])
            return self._approval_view(a)

    # ---- browser approval page (no bearer token: the console code is the proof that a human decided)
    def ui_list(self, limit: int = 30) -> list[dict]:
        with self.lock:
            views = [self._approval_view(a).model_dump(mode="json") for a in self.approvals.values()]
        return sorted(views, key=lambda v: v["created_at"], reverse=True)[:limit]

    def ui_record(self, approval_id: str) -> dict:
        with self.lock:
            a = self.approvals.get(approval_id)
            if a is None:
                raise ApiError(404, ErrorCode.APPROVAL_NOT_FOUND, "no such approval")
            view = self._approval_view(a).model_dump(mode="json")
            return {**view, "payload": a.get("payload") or {}, "form_token": a.get("form_token", "")}

    def grafana_links(self, incident_id: str) -> list[tuple[str, str]]:
        if not self.grafana_url:
            return []
        try:
            from grafana_dash import INDEX_UID, dashboard_uids
        except Exception:  # noqa: BLE001 - links are a convenience only
            return []
        rc, ver = dashboard_uids(incident_id)
        base = self.grafana_url if self.grafana_url.endswith("/grafana") else self.grafana_url + "/grafana"
        return [("Root cause and cited evidence", f"{base}/d/{rc}"), ("Action API request and verification", f"{base}/d/{ver}"),
                ("All incidents", f"{base}/d/{INDEX_UID}")]

    def decide_on_page(self, approval_id: str, *, approve: bool, code: str, approver: str, form_token: str) -> str:
        with self.lock:
            a = self.approvals.get(approval_id)
            if a is None:
                raise ApiError(404, ErrorCode.APPROVAL_NOT_FOUND, "no such approval")
            if not a.get("form_token") or not hmac.compare_digest(a["form_token"].encode(), form_token.encode()):
                self._audit("refused_page_decision", approval_id=approval_id, reason="form token mismatch")
                raise ApiError(403, ErrorCode.UNAUTHORIZED, "stale or forged form; reload the approval page")
            status = self._approval_view(a).status
            if status != "pending":
                return f"This request is already {status}; nothing changed."
            who = printable(approver, 80).strip() or "approval page"
            if not approve:
                a["status"] = "rejected"
                self._save()
                self._audit("approval_rejected", approval_id=approval_id, by=who, reason="rejected on the approval page")
                return "Rejected. Nothing will run; the agent records the rejection."
            norm = code.strip().upper()
            if not _FORM_CODE.match(norm) or not hmac.compare_digest(a["code_hash"], _code_hash(approval_id, norm)):
                a["failed_attempts"] += 1
                if a["failed_attempts"] >= MAX_CODE_ATTEMPTS:
                    a["status"] = "locked"
                self._save()
                self._audit("refused_approval", approval_id=approval_id, reason=ErrorCode.APPROVAL_CODE_INVALID.value,
                            attempts=a["failed_attempts"], via="approval page")
                if a["status"] == "locked":
                    return "Wrong code. The request is now locked; nothing will run."
                return (f"Wrong code ({a['failed_attempts']} of {MAX_CODE_ATTEMPTS} attempts used). Copy the ONE-TIME "
                        "APPROVAL CODE for this approval id from the Action API terminal.")
            a["status"], a["approved_by"] = "approved", who
            self._save()
            self._audit("approval_granted", approval_id=approval_id, incident_id=a["incident_id"], by=who,
                        via="approval page", payload_digest=a["payload_digest"])
            return "Approved. The agent will now re-check the system and run exactly this action once."

    # ---- execution
    def execute(self, req: ExecuteRequest, remote_addr: Optional[str] = None) -> tuple[int, ActionRecord]:
        with self.lock:
            payload = self._normalized(req.payload)
            digest = payload.digest()
            existing = next((r for r in self.actions.values() if r.idempotency_key == req.idempotency_key), None)
            if existing is not None:
                if existing.payload_digest != digest:
                    raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency key reused with a different payload")
                self._audit("idempotent_replay", action_id=existing.action_id, idempotency_key=req.idempotency_key)
                return 200, existing
            if self.kill_switch():
                self._audit("refused_kill_switch", incident_id=payload.incident_id, payload_digest=digest)
                raise ApiError(423, ErrorCode.KILL_SWITCH_ACTIVE, "kill switch is active: no state-changing actions")
            if any(r.incident_id == payload.incident_id for r in self.actions.values()):
                raise ApiError(409, ErrorCode.INCIDENT_ACTION_EXISTS, "an action was already executed for this incident")
            self._authorize(req, payload, digest)
            rec = ActionRecord(
                action_id=f"act-{uuid.uuid4().hex[:16]}", incident_id=payload.incident_id, operation=payload.operation,
                target=payload.target, arguments=payload.arguments, evidence_ids=payload.evidence_ids,
                reason=payload.reason, payload_digest=digest, proposal_digest=payload.proposal_digest,
                idempotency_key=req.idempotency_key, status="accepted", executor=self.executor.mode, created_at=self.clock(),
                audit=AuditInfo(approval_id=req.approval_id, requested_by=self.approvals[req.approval_id]["requested_by"],
                                remote_addr=remote_addr))
            self.actions[rec.action_id] = rec
            self._save()
            self._audit("action_accepted", action_id=rec.action_id, incident_id=rec.incident_id, operation=rec.operation,
                        target=rec.target, arguments=rec.arguments, approval_id=req.approval_id, payload_digest=digest,
                        executor=self.executor.mode)
        if self.run_async:
            threading.Thread(target=self._run, args=(rec.action_id,), daemon=True).start()
        else:
            self._run(rec.action_id)
        return 202, self.actions[rec.action_id] if not self.run_async else rec

    def _authorize(self, req: ExecuteRequest, payload: ActionPayload, digest: str) -> None:
        a = self.approvals.get(req.approval_id)
        if a is None:
            raise ApiError(403, ErrorCode.APPROVAL_NOT_FOUND, "approval does not exist")
        view = self._approval_view(a)
        refusals = {"consumed": (409, ErrorCode.APPROVAL_CONSUMED, "approval already used"),
                    "rejected": (403, ErrorCode.APPROVAL_REJECTED, "approval was rejected"),
                    "expired": (403, ErrorCode.APPROVAL_EXPIRED, "approval expired"),
                    "locked": (403, ErrorCode.APPROVAL_LOCKED, "approval locked after repeated invalid codes")}
        if view.status in refusals:
            s, c, m = refusals[view.status]
            self._audit("refused_approval", approval_id=req.approval_id, reason=c.value)
            raise ApiError(s, c, m)
        if view.status == "approved":  # a human entered the console code on the approval page
            if a["payload_digest"] != digest:
                self._audit("refused_approval", approval_id=req.approval_id, reason=ErrorCode.PAYLOAD_MISMATCH.value)
                raise ApiError(403, ErrorCode.PAYLOAD_MISMATCH, "payload differs from the approved action")
            a["status"] = "consumed"
            self._save()
            return
        if not req.approval_code:
            self._audit("refused_approval", approval_id=req.approval_id, reason=ErrorCode.APPROVAL_NOT_GRANTED.value)
            raise ApiError(403, ErrorCode.APPROVAL_NOT_GRANTED, "no human approval yet: approve on the approval page or supply the code")
        if not hmac.compare_digest(a["code_hash"], _code_hash(req.approval_id, req.approval_code)) or a["payload_digest"] != digest:
            mismatch = a["payload_digest"] != digest
            a["failed_attempts"] += 1
            if a["failed_attempts"] >= MAX_CODE_ATTEMPTS:
                a["status"] = "locked"
            self._save()
            code = ErrorCode.PAYLOAD_MISMATCH if mismatch else ErrorCode.APPROVAL_CODE_INVALID
            self._audit("refused_approval", approval_id=req.approval_id, reason=code.value, attempts=a["failed_attempts"])
            raise ApiError(403, code, "payload differs from the approved action" if mismatch else "invalid approval code")
        a["status"] = "consumed"
        self._save()

    def _run(self, action_id: str) -> None:
        with self.lock:
            rec = self.actions[action_id]
            if self.kill_switch():
                self._finish(rec, "blocked", ExecutionResult(success=False, detail="kill switch activated before execution"))
                return
            rec = rec.model_copy(update={"status": "running", "started_at": self.clock()})
            self.actions[action_id] = rec
            self._save()
        try:
            result = self.executor.restart(rec.target, int(rec.arguments.get("grace_period_seconds", 10)))
        except Exception as exc:  # executor bugs must not leave the record running
            result = ExecutionResult(success=False, detail=f"executor error: {type(exc).__name__}")
        status = "simulated" if result.simulated else "succeeded" if result.success else "failed"
        with self.lock:
            self._finish(self.actions[action_id], status, result)

    def _finish(self, rec: ActionRecord, status: str, result: ExecutionResult) -> None:
        rec = rec.model_copy(update={"status": status, "result": result, "finished_at": self.clock()})
        self.actions[rec.action_id] = rec
        self._save()
        self._audit("action_finished", action_id=rec.action_id, incident_id=rec.incident_id, status=status,
                    detail=result.detail, simulated=result.simulated)

    def record_verification(self, action_id: str, report: VerificationReport) -> ActionRecord:
        with self.lock:
            rec = self.get_action(action_id)
            if rec.verification is not None:
                raise ApiError(409, ErrorCode.VERIFICATION_EXISTS, "verification already recorded")
            rec = rec.model_copy(update={"verification": report})
            self.actions[action_id] = rec
            self._save()
            self._audit("verification_recorded", action_id=action_id, outcome=report.outcome, by=report.recorded_by)
            return rec


# ---------------------------------------------------------------- HTTP layer
def make_handler(service: ActionApiService, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "IncidentZeroActionAPI/" + API_VERSION
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # never log headers (bearer token)
            sys.stderr.write(f"[action-api] {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}\n")

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body.model_dump(mode="json") if isinstance(body, BaseModel) else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _auth(self) -> None:
            got = self.headers.get("Authorization", "")
            if not got.startswith("Bearer ") or not hmac.compare_digest(got[7:].encode(), token.encode()):
                raise ApiError(401, ErrorCode.UNAUTHORIZED, "missing or invalid bearer token")

        def _body(self, model: type[BaseModel]) -> Any:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise ApiError(415, ErrorCode.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, ErrorCode.INVALID_REQUEST, "invalid Content-Length")
            if length > MAX_BODY:
                raise ApiError(413, ErrorCode.PAYLOAD_TOO_LARGE, f"body exceeds {MAX_BODY} bytes")
            try:
                return model.model_validate_json(self.rfile.read(length))
            except ValidationError as exc:
                raise ApiError(400, ErrorCode.INVALID_REQUEST, "request does not match the schema",
                               [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:8]])

        def _html(self, status: int, page: str) -> None:
            data = page.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", approval_ui.CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _form(self) -> dict[str, str]:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/x-www-form-urlencoded":
                raise ApiError(415, ErrorCode.UNSUPPORTED_MEDIA_TYPE, "form expected")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, ErrorCode.INVALID_REQUEST, "invalid Content-Length")
            if length > MAX_FORM_BODY:
                raise ApiError(413, ErrorCode.PAYLOAD_TOO_LARGE, "form too large")
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return {k: v[0] for k, v in parse_qs(raw, max_num_fields=8).items()}

        def _ui(self, method: str, path: str) -> None:
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
            if self.headers.get("Host", "") not in hosts:  # DNS-rebinding guard
                return self._html(400, approval_ui.message_page("Wrong host", f"Open http://127.0.0.1:{port}/ui"))
            parts = path.split("/")[2:]
            try:
                if method == "GET" and not parts:
                    return self._html(200, approval_ui.index_page(service.ui_list()))
                if len(parts) != 2 or parts[0] != "approvals" or not _APPROVAL_PATH_ID.match(parts[1]):
                    return self._html(404, approval_ui.message_page("Not found", "No such page."))
                message = ""
                if method == "POST":
                    origin = self.headers.get("Origin")
                    if origin and origin not in {f"http://{h}" for h in hosts}:
                        return self._html(403, approval_ui.message_page("Refused", "Cross-site form submission refused."))
                    form = self._form()
                    message = service.decide_on_page(parts[1], approve=form.get("decision") == "approve",
                                                     code=form.get("code", ""), approver=form.get("approver", ""),
                                                     form_token=form.get("form_token", ""))
                rec = service.ui_record(parts[1])
                return self._html(200, approval_ui.approval_page(rec, message, service.grafana_links(rec["incident_id"])))
            except ApiError as e:
                return self._html(e.status, approval_ui.message_page("Refused", e.message))

        def _route(self, method: str) -> None:
            path = self.path.split("?")[0].rstrip("/") or "/"
            try:
                if method == "GET" and path == "/health":
                    return self._send(200, service.health())
                if path == "/ui" or path.startswith("/ui/"):
                    return self._ui(method, path)
                if not path.startswith("/v1/"):
                    raise ApiError(404, ErrorCode.NOT_FOUND, "unknown path")
                self._auth()
                parts = path.split("/")[2:]
                if method == "GET" and parts == ["operations"]:
                    return self._send(200, service.list_operations())
                if method == "GET" and parts == ["services"]:
                    return self._send(200, service.system_state())
                if method == "GET" and parts == ["actions"]:
                    return self._send(200, service.list_actions())
                if method == "POST" and parts == ["actions", "validate"]:
                    return self._send(200, service.validate(self._body(ActionPayload)))
                if method == "POST" and parts == ["approvals"]:
                    return self._send(201, service.create_approval(self._body(ApprovalCreateRequest)))
                if method == "GET" and len(parts) == 2 and parts[0] == "approvals":
                    return self._send(200, service.get_approval(parts[1]))
                if method == "POST" and len(parts) == 3 and parts[0] == "approvals" and parts[2] == "reject":
                    return self._send(200, service.reject_approval(parts[1], self._body(ApprovalRejectRequest)))
                if method == "POST" and parts == ["actions", "execute"]:
                    status, rec = service.execute(self._body(ExecuteRequest), self.client_address[0])
                    return self._send(status, rec)
                if len(parts) >= 2 and parts[0] == "actions":
                    if method == "GET" and len(parts) == 2:
                        return self._send(200, service.get_action(parts[1]))
                    if method == "GET" and parts[2:] == ["result"]:
                        return self._send(200, service.result(parts[1]))
                    if method == "POST" and parts[2:] == ["verification"]:
                        return self._send(200, service.record_verification(parts[1], self._body(VerificationReport)))
                raise ApiError(404, ErrorCode.NOT_FOUND, "unknown path")
            except ApiError as e:
                self._send(e.status, e.body())
            except Exception as exc:  # noqa: BLE001 - never leak internals
                self._send(500, ApiError(500, ErrorCode.INTERNAL_ERROR, f"internal error: {type(exc).__name__}").body())

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

    return Handler


def build_server(service: ActionApiService, token: str, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    if len(token) < 24:
        raise ValueError("ACTION_API_TOKEN must be at least 24 characters (use: python cli.py action-api init-token)")
    return ThreadingHTTPServer((host, port), make_handler(service, token))
