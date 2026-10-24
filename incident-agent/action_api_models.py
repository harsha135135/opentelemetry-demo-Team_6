"""Team-defined Action API: request/response models. These Pydantic models are the single source of truth for the
JSON Schemas in schemas/action_api/ and for docs/action_api.openapi.json (regenerate with
`python cli.py action-api export-schemas`; tests fail on drift).

This is our team's API, not an organiser-supplied contract.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from typing_extensions import Annotated

API_VERSION = "1.0.0"
INCIDENT_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
OPERATION = r"^[a-z][a-z0-9_]{0,47}$"
TARGET = r"^[a-z0-9][a-z0-9-]{0,62}$"
EVIDENCE_ID = r"^ev-[A-Za-z0-9._-]{1,96}$"
IDEMPOTENCY_KEY = r"^[A-Za-z0-9._:-]{8,128}$"
APPROVAL_ID = r"^apr-[a-f0-9]{16}$"
ACTION_ID = r"^act-[a-f0-9]{16}$"
APPROVAL_CODE = r"^[A-Z2-9]{4}-?[A-Z2-9]{4}$"
DIGEST = r"^[a-f0-9]{64}$"

Scalar = Union[bool, int, float, str]


def no_control_chars(text: str) -> str:
    """Reject terminal control characters (ESC, CR, ...) in free text that is shown to the approving human."""
    bad = [c for c in text if (ord(c) < 32 and c not in "\n\t") or 0x7F <= ord(c) <= 0x9F]
    if bad:
        raise ValueError("control characters are not allowed in free text")
    return text


def printable(text: object, limit: int = 2000) -> str:
    """Render untrusted text for a terminal: one line, every control character escaped."""
    out = []
    for c in str(text)[:limit]:
        if c in "\n\t":
            out.append(" ")
        elif ord(c) < 32 or 0x7F <= ord(c) <= 0x9F:
            out.append(f"\\x{ord(c):02x}")
        else:
            out.append(c)
    return "".join(out)


SafeText = Annotated[str, AfterValidator(no_control_chars)]
NO_CONTROL = r"^[^\x00-\x08\x0b-\x1f\x7f-\x9f]*$"  # documents the same rule in the JSON Schema


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    TARGET_NOT_ALLOWED = "TARGET_NOT_ALLOWED"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_LOCKED = "APPROVAL_LOCKED"
    APPROVAL_CODE_INVALID = "APPROVAL_CODE_INVALID"
    PAYLOAD_MISMATCH = "PAYLOAD_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INCIDENT_ACTION_EXISTS = "INCIDENT_ACTION_EXISTS"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    VERIFICATION_EXISTS = "VERIFICATION_EXISTS"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(_Strict):
    code: ErrorCode
    message: str
    details: list[str] = Field(default_factory=list)


class ErrorResponse(_Strict):
    error: ErrorDetail


class ActionPayload(_Strict):
    """The exact action. Approval is bound to the SHA-256 digest of this object's canonical JSON."""
    incident_id: str = Field(pattern=INCIDENT_ID)
    operation: str = Field(pattern=OPERATION)
    target: str = Field(pattern=TARGET)
    arguments: dict[str, Scalar] = Field(default_factory=dict, max_length=10)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    reason: SafeText = Field(min_length=10, max_length=1500, pattern=NO_CONTROL)
    proposal_digest: Optional[str] = Field(None, pattern=DIGEST, description="agent-side proposal digest, for audit correlation")

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ValidateResponse(_Strict):
    valid: bool
    payload_digest: Optional[str] = None
    normalized_arguments: dict[str, Scalar] = Field(default_factory=dict)
    errors: list[ErrorDetail] = Field(default_factory=list)


class ApprovalCreateRequest(_Strict):
    payload: ActionPayload
    requested_by: SafeText = Field(min_length=1, max_length=80, pattern=NO_CONTROL)
    ttl_seconds: int = Field(120, ge=30, le=300)


ApprovalStatus = Literal["pending", "consumed", "rejected", "expired", "locked"]


class Approval(_Strict):
    """An approval request. The one-time code is printed only on the Action API operator console; it is never returned."""
    approval_id: str = Field(pattern=APPROVAL_ID)
    status: ApprovalStatus
    incident_id: str
    operation: str
    target: str
    payload_digest: str = Field(pattern=DIGEST)
    requested_by: str
    created_at: datetime
    expires_at: datetime
    failed_attempts: int = 0
    code_delivery: Literal["action-api-console"] = "action-api-console"


class ApprovalRejectRequest(_Strict):
    rejected_by: str = Field(min_length=1, max_length=80)
    reason: str = Field("", max_length=500)


class ExecuteRequest(_Strict):
    payload: ActionPayload
    approval_id: str = Field(pattern=APPROVAL_ID)
    approval_code: str = Field(pattern=APPROVAL_CODE)
    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY)


ActionStatus = Literal["accepted", "running", "succeeded", "failed", "simulated", "blocked", "unknown"]
TERMINAL_STATUSES = {"succeeded", "failed", "simulated", "blocked", "unknown"}


class ExecutionResult(_Strict):
    success: bool
    simulated: bool = False
    detail: str
    status_before: Optional[str] = None
    status_after: Optional[str] = None
    exit_code: Optional[int] = None
    duration_s: Optional[float] = None


VerificationOutcome = Literal["VERIFIED_RECOVERY", "PARTIAL_RECOVERY", "NOT_RECOVERED", "UNVERIFIED", "ACTION_FAILED",
                              "ESCALATED", "DRY_RUN"]


class VerificationReport(_Strict):
    outcome: VerificationOutcome
    details: str = Field(max_length=6000)
    baseline_evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    post_action_evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    recorded_by: str = Field(min_length=1, max_length=80)


class AuditInfo(_Strict):
    approved_by_code_holder: bool = True
    approval_id: str
    requested_by: str
    remote_addr: Optional[str] = None


class ActionRecord(_Strict):
    action_id: str = Field(pattern=ACTION_ID)
    incident_id: str
    operation: str
    target: str
    arguments: dict[str, Scalar]
    evidence_ids: list[str]
    reason: str
    payload_digest: str
    proposal_digest: Optional[str] = None
    idempotency_key: str
    status: ActionStatus
    executor: Literal["docker", "simulate"]
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[ExecutionResult] = None
    verification: Optional[VerificationReport] = None
    audit: AuditInfo


class ActionResultResponse(_Strict):
    action_id: str
    status: ActionStatus
    terminal: bool
    result: Optional[ExecutionResult] = None
    finished_at: Optional[datetime] = None


class ActionList(_Strict):
    actions: list[ActionRecord]


class OperationSpec(_Strict):
    name: str
    description: str
    target_kind: Literal["service"] = "service"
    allowed_targets: list[str]
    parameters: dict[str, dict[str, Any]]
    required: list[str] = Field(default_factory=list)
    risk: str
    reversible: bool
    idempotent: bool
    expected_effect: str


class OperationList(_Strict):
    api_version: str
    provenance: Literal["team"] = "team"
    executor: Literal["docker", "simulate"]
    operations: list[OperationSpec]


class Health(_Strict):
    status: Literal["ok"] = "ok"
    api_version: str
    executor: Literal["docker", "simulate"]
    kill_switch_active: bool
    time: datetime


SCHEMA_MODELS = {
    "ActionPayload": ActionPayload, "ValidateResponse": ValidateResponse, "ApprovalCreateRequest": ApprovalCreateRequest,
    "Approval": Approval, "ApprovalRejectRequest": ApprovalRejectRequest, "ExecuteRequest": ExecuteRequest,
    "ActionRecord": ActionRecord, "ActionResultResponse": ActionResultResponse, "ActionList": ActionList,
    "VerificationReport": VerificationReport, "OperationList": OperationList, "Health": Health,
    "ErrorResponse": ErrorResponse,
}


def json_schemas() -> dict[str, dict[str, Any]]:
    out = {}
    for name, model in SCHEMA_MODELS.items():
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://incident-zero.team6.local/schemas/action_api/{name}.schema.json"
        out[name] = schema
    return out


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json(name: str) -> dict[str, Any]:
    return {"content": {"application/json": {"schema": _ref(name)}}}


def openapi() -> dict[str, Any]:
    components: dict[str, Any] = {}
    for name, model in SCHEMA_MODELS.items():
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for dname, dschema in schema.pop("$defs", {}).items():
            components[dname] = dschema
        components[name] = schema
    err = {"description": "Error (see ErrorCode)", **_json("ErrorResponse")}
    errors = {str(c): err for c in (400, 401, 403, 404, 409, 413, 415, 423)}
    secured = [{"bearerAuth": []}]

    def op(summary, ok_code, ok_schema, body=None, extra=None):
        o: dict[str, Any] = {"summary": summary, "security": secured,
                             "responses": {ok_code: {"description": "OK", **_json(ok_schema)}, **errors}}
        if body:
            o["requestBody"] = {"required": True, **_json(body)}
        if extra:
            o["responses"].update(extra)
        return o

    path_id = lambda n, p: {"name": n, "in": "path", "required": True, "schema": {"type": "string", "pattern": p}}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Incident Zero Team 6 Action API", "version": API_VERSION,
                 "description": "Team-defined remediation API for the AI on-call engineer. Not an organiser contract. "
                                "State-changing calls require a single-use approval code that is shown only on the "
                                "Action API operator console."},
        "servers": [{"url": "http://127.0.0.1:8787"}],
        "components": {"schemas": components,
                       "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer",
                                                          "description": "ACTION_API_TOKEN (service-to-service)"}}},
        "paths": {
            "/health": {"get": {"summary": "Liveness, executor mode and kill-switch state", "security": [],
                                "responses": {"200": {"description": "OK", **_json("Health")}}}},
            "/v1/operations": {"get": op("Allowlisted operations with argument schemas and targets", "200", "OperationList")},
            "/v1/actions": {"get": op("Recent action records", "200", "ActionList")},
            "/v1/actions/validate": {"post": op("Stateless validation of an action payload (dry-run)", "200",
                                                "ValidateResponse", "ActionPayload")},
            "/v1/approvals": {"post": op("Create an approval request bound to the payload digest; prints a one-time "
                                         "code on the operator console", "201", "Approval", "ApprovalCreateRequest")},
            "/v1/approvals/{approval_id}": {"get": {**op("Approval status (never the code)", "200", "Approval"),
                                                    "parameters": [path_id("approval_id", APPROVAL_ID)]}},
            "/v1/approvals/{approval_id}/reject": {"post": {**op("Record a human rejection", "200", "Approval",
                                                                 "ApprovalRejectRequest"),
                                                            "parameters": [path_id("approval_id", APPROVAL_ID)]}},
            "/v1/actions/execute": {"post": op("Execute an approved action asynchronously", "202", "ActionRecord",
                                               "ExecuteRequest",
                                               {"200": {"description": "Idempotent replay: existing record",
                                                        **_json("ActionRecord")}})},
            "/v1/actions/{action_id}": {"get": {**op("Full action record", "200", "ActionRecord"),
                                                "parameters": [path_id("action_id", ACTION_ID)]}},
            "/v1/actions/{action_id}/result": {"get": {**op("Execution status/result for polling", "200",
                                                            "ActionResultResponse"),
                                                       "parameters": [path_id("action_id", ACTION_ID)]}},
            "/v1/actions/{action_id}/verification": {"post": {**op("Attach the agent's recovery verification", "200",
                                                                   "ActionRecord", "VerificationReport"),
                                                              "parameters": [path_id("action_id", ACTION_ID)]}},
        },
    }


def export(root: Path) -> list[Path]:
    """Write schemas/action_api/*.schema.json and docs/action_api.openapi.json under root (incident-agent/)."""
    written = []
    sdir = root / "schemas" / "action_api"
    sdir.mkdir(parents=True, exist_ok=True)
    for name, schema in json_schemas().items():
        p = sdir / f"{name}.schema.json"
        p.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(p)
    p = root / "docs" / "action_api.openapi.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written.append(p)
    return written
