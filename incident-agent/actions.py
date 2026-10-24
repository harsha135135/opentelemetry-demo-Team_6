"""Action API adapter: operation catalog, argument schemas and the only code path that sends a state-changing request.

No official organiser contract is available yet. The catalog file (ACTION_API_CATALOG) must be written from that
contract. Catalogs with provenance "mock" exist for tests and dry-run demonstrations only: live dispatch refuses them.
Without a catalog the planner is offered no operations and can only escalate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_PATH = re.compile(r"^/[A-Za-z0-9/_.{}-]{0,200}$")
_SUPPORTED_SCHEMA_KEYS = {"type", "description", "enum", "minimum", "maximum", "minLength", "maxLength", "pattern", "default"}
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


class CatalogError(ValueError):
    pass


class Operation(BaseModel):
    name: str
    description: str
    method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST"
    path: str
    target_kind: Literal["service", "flag", "none"] = "service"
    target_field: str = "target"
    allowed_targets: list[str] = Field(default_factory=list)
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    risk: str
    reversible: bool = False
    rollback: Optional[str] = None
    idempotent: bool = False
    reduces_load: bool = False  # e.g. throttling traffic: recovery can only be partial
    expected_effect: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Operation":
        if not _NAME.match(self.name):
            raise ValueError(f"invalid operation name {self.name!r}")
        if not _PATH.match(self.path):
            raise ValueError(f"invalid path for {self.name}")
        if self.target_kind != "none" and not self.allowed_targets:
            raise ValueError(f"{self.name}: allowed_targets must list explicit targets")
        for pname, schema in self.parameters.items():
            if not _NAME.match(pname):
                raise ValueError(f"{self.name}: invalid parameter name {pname!r}")
            unknown = set(schema) - _SUPPORTED_SCHEMA_KEYS
            if unknown or schema.get("type") not in _TYPES:
                raise ValueError(f"{self.name}.{pname}: unsupported schema {sorted(unknown) or schema.get('type')}")
        if set(self.required) - set(self.parameters):
            raise ValueError(f"{self.name}: required parameters not declared")
        return self

    def json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.parameters, "required": self.required, "additionalProperties": False}

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


class Catalog(BaseModel):
    provenance: Literal["organiser", "team", "mock"]  # team = our own Action API (action_server.py), not an organiser contract
    description: str = ""
    base_url_env: str = "ACTION_API_URL"
    auth_header: Optional[str] = None  # e.g. "Authorization"; value comes from token_env, never from the file
    auth_scheme: str = "Bearer"
    token_env: Optional[str] = None
    timeout_s: float = Field(15, gt=0, le=60)
    operations: list[Operation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "Catalog":
        names = [o.name for o in self.operations]
        if len(names) != len(set(names)):
            raise ValueError("duplicate operation names")
        return self

    def get(self, name: str) -> Optional[Operation]:
        return next((o for o in self.operations if o.name == name), None)

    @property
    def base_url(self) -> str:
        return os.getenv(self.base_url_env, "").rstrip("/")


EMPTY_CATALOG = Catalog(provenance="organiser", description="no Action API contract configured", operations=[])


def load_catalog(path: str | os.PathLike | None) -> Catalog:
    """Load and validate a catalog; a missing path yields the empty catalog (escalation only)."""
    if not path:
        return EMPTY_CATALOG
    try:
        return Catalog.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise CatalogError(f"action catalog {path} is invalid: {str(exc)[:400]}") from exc


def validate_arguments(op: Operation, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Strict check of model-proposed arguments against the operation's declared parameters."""
    errors: list[str] = []
    if not isinstance(arguments, dict):
        return {}, ["arguments must be an object"]
    unknown = set(arguments) - set(op.parameters)
    if unknown:
        errors.append(f"unknown arguments for {op.name}: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for name, schema in op.parameters.items():
        if name not in arguments:
            if name in op.required:
                errors.append(f"missing required argument '{name}'")
            elif "default" in schema:
                clean[name] = schema["default"]
            continue
        v = arguments[name]
        kind = schema["type"]
        if isinstance(v, bool) and kind != "boolean" or not isinstance(v, _TYPES[kind]):
            errors.append(f"argument '{name}' must be {kind}")
            continue
        if "enum" in schema and v not in schema["enum"]:
            errors.append(f"argument '{name}' must be one of {schema['enum']}")
        if "minimum" in schema and v < schema["minimum"] or "maximum" in schema and v > schema["maximum"]:
            errors.append(f"argument '{name}' out of range [{schema.get('minimum')}, {schema.get('maximum')}]")
        if kind == "string":
            if len(v) < schema.get("minLength", 0) or len(v) > schema.get("maxLength", 200):
                errors.append(f"argument '{name}' has invalid length")
            if "pattern" in schema and not re.fullmatch(schema["pattern"], v):
                errors.append(f"argument '{name}' does not match the declared pattern")
        clean[name] = v
    return clean, errors


class ActionResult(BaseModel):
    outcome: Literal["success", "failed", "unknown"]
    simulated: bool = False  # the executor changed nothing (team API simulate mode)
    refused: bool = False    # rejected before execution (authorization, kill switch, unavailable); nothing ran
    http_status: Optional[int] = None
    request_id: Optional[str] = None
    response: Optional[dict[str, Any]] = None
    detail: str = ""


class ActionClient(Protocol):
    def execute(self, op: Operation, target: Optional[str], arguments: dict[str, Any], idempotency_key: str) -> ActionResult: ...


def build_request(catalog: Catalog, op: Operation, target: Optional[str], arguments: dict[str, Any]) -> dict[str, Any]:
    """The exact request that would be sent; also shown in dry-run and approval output. Never contains secrets."""
    body = dict(arguments)
    if op.target_kind != "none":
        body[op.target_field] = target
    return {"method": op.method, "url": f"{catalog.base_url or '<ACTION_API_URL unset>'}{op.path}", "json": body}


class HttpActionClient:
    """Sends one request per approved action. Never follows redirects. Retries only connection failures (request not
    sent) for operations the contract marks idempotent; a timeout after sending is an unknown outcome, never retried."""

    def __init__(self, catalog: Catalog, transport: httpx.BaseTransport | None = None) -> None:
        if catalog.provenance != "organiser":
            raise CatalogError("live execution requires an organiser-provided catalog; this one is a mock")
        if not catalog.base_url:
            raise CatalogError(f"{catalog.base_url_env} is not set")
        self.catalog = catalog
        self.transport = transport

    def _headers(self, idempotency_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}
        if self.catalog.auth_header and self.catalog.token_env:
            token = os.getenv(self.catalog.token_env, "")
            if not token:
                raise CatalogError(f"{self.catalog.token_env} is not set")
            headers[self.catalog.auth_header] = f"{self.catalog.auth_scheme} {token}".strip()
        return headers

    def execute(self, op: Operation, target: Optional[str], arguments: dict[str, Any], idempotency_key: str) -> ActionResult:
        req = build_request(self.catalog, op, target, arguments)
        attempts = 2 if op.idempotent else 1
        with httpx.Client(timeout=self.catalog.timeout_s, follow_redirects=False, transport=self.transport) as client:
            for attempt in range(1, attempts + 1):
                try:
                    r = client.request(req["method"], req["url"], json=req["json"], headers=self._headers(idempotency_key))
                except httpx.ConnectError as exc:
                    if attempt < attempts:
                        continue
                    return ActionResult(outcome="failed", detail=f"Action API not reachable: {type(exc).__name__}")
                except httpx.TimeoutException as exc:
                    return ActionResult(outcome="unknown", detail=f"timed out after sending ({type(exc).__name__}); outcome unknown")
                except httpx.HTTPError as exc:
                    return ActionResult(outcome="unknown", detail=f"transport error after sending: {type(exc).__name__}")
                try:
                    body = r.json() if r.content else {}
                    body = body if isinstance(body, dict) else {"body": body}
                except ValueError:
                    body = {"body": r.text[:500]}
                rid = next((str(body[k]) for k in ("request_id", "requestId", "id", "action_id") if body.get(k)), None)
                rid = rid or r.headers.get("x-request-id")
                if 200 <= r.status_code < 300:
                    return ActionResult(outcome="success", http_status=r.status_code, request_id=rid, response=body)
                if 400 <= r.status_code < 500:
                    return ActionResult(outcome="failed", http_status=r.status_code, request_id=rid, response=body,
                                        detail="Action API rejected the request")
                return ActionResult(outcome="unknown", http_status=r.status_code, request_id=rid, response=body,
                                    detail="Action API server error; the action may or may not have been applied")
        return ActionResult(outcome="unknown", detail="no response")  # pragma: no cover
