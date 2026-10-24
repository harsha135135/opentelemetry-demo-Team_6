"""Agent-side client for the team Action API. Deterministic code only; never offered to the LLM.

Live execution needs a server-issued approval: the agent opens an approval for the exact payload, the human reads the
one-time code on the Action API console and types it into the agent prompt or onto the server's approval page, and the
server verifies it. With the page, the agent only polls the approval status and never handles the code.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

import httpx

from action_api_models import ActionPayload, Approval, OperationList, VerificationReport
from actions import ActionResult, Catalog, Operation


class ActionApiError(RuntimeError):
    def __init__(self, code: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.status = code, status


class TeamActionApiClient:
    supports_server_approval = True

    def __init__(self, base_url: str, token: str, *, timeout_s: float = 10, poll_timeout_s: float = 120,
                 poll_interval_s: float = 1.0, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not base_url:
            raise ActionApiError("NOT_CONFIGURED", "ACTION_API_URL is not set")
        if not token:
            raise ActionApiError("NOT_CONFIGURED", "ACTION_API_TOKEN is not set")
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.timeout_s, self.poll_timeout_s, self.poll_interval_s = timeout_s, poll_timeout_s, poll_interval_s
        self.transport, self.sleep = transport, sleep

    def _call(self, method: str, path: str, body: Any = None) -> tuple[int, dict]:
        with httpx.Client(timeout=self.timeout_s, follow_redirects=False, transport=self.transport) as c:
            content = None if body is None else body.model_dump_json() if hasattr(body, "model_dump_json") else json.dumps(body)
            r = c.request(method, f"{self.base_url}{path}", headers=self._headers, content=content)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            err = (data or {}).get("error") or {}
            raise ActionApiError(err.get("code", f"HTTP_{r.status_code}"),
                                 err.get("message", "request failed") + ("; " + "; ".join(err.get("details", [])) if err.get("details") else ""),
                                 r.status_code)
        return r.status_code, data

    def health(self) -> dict:
        with httpx.Client(timeout=self.timeout_s, transport=self.transport) as c:
            r = c.get(f"{self.base_url}/health")
            r.raise_for_status()
            return r.json()

    def operations(self) -> OperationList:
        return OperationList.model_validate(self._call("GET", "/v1/operations")[1])

    def catalog(self) -> Catalog:
        ol = self.operations()
        ops = [Operation(name=o.name, description=o.description, method="POST", path="/v1/actions/execute",
                         target_kind="service", target_field="target", allowed_targets=o.allowed_targets,
                         parameters=o.parameters, required=o.required, risk=o.risk, reversible=o.reversible,
                         idempotent=o.idempotent, expected_effect=o.expected_effect) for o in ol.operations]
        return Catalog(provenance="team", description=f"team Action API {ol.api_version} (executor: {ol.executor})",
                       base_url_env="ACTION_API_URL", operations=ops)

    def validate(self, payload: ActionPayload) -> dict:
        return self._call("POST", "/v1/actions/validate", payload)[1]

    def open_approval(self, payload: ActionPayload, requested_by: str, ttl_s: float) -> Approval:
        body = {"payload": payload.model_dump(mode="json"), "requested_by": requested_by[:80] or "operator",
                "ttl_seconds": int(min(300, max(30, ttl_s)))}
        return Approval.model_validate(self._call("POST", "/v1/approvals", body)[1])

    def approval_status(self, approval_id: str) -> Approval:
        return Approval.model_validate(self._call("GET", f"/v1/approvals/{approval_id}")[1])

    def approval_page_url(self, approval: Approval) -> str:
        return f"{self.base_url}{approval.approval_page or '/ui/approvals/' + approval.approval_id}"

    def reject_approval(self, approval_id: str, by: str, reason: str) -> None:
        self._call("POST", f"/v1/approvals/{approval_id}/reject", {"rejected_by": by[:80] or "operator", "reason": reason[:500]})

    def execute_approved(self, payload: ActionPayload, approval_id: str, code: Optional[str], idempotency_key: str) -> ActionResult:
        body = {"payload": payload.model_dump(mode="json"), "approval_id": approval_id, "approval_code": code,
                "idempotency_key": idempotency_key}
        try:
            _, rec = self._call("POST", "/v1/actions/execute", body)
        except httpx.ConnectError:
            return ActionResult(outcome="failed", refused=True, detail="Action API unavailable (request not sent)")
        except httpx.TimeoutException:
            return ActionResult(outcome="unknown", detail="Action API timed out after the request was sent; outcome unknown")
        except ActionApiError as e:
            if e.status is not None and e.status >= 500:
                return ActionResult(outcome="unknown", http_status=e.status, detail=str(e))
            return ActionResult(outcome="failed", refused=True, http_status=e.status, detail=f"Action API refused: {e}")
        return self._poll(rec["action_id"])

    def _poll(self, action_id: str) -> ActionResult:
        waited = 0.0
        while True:
            try:
                _, res = self._call("GET", f"/v1/actions/{action_id}/result")
            except (httpx.HTTPError, ActionApiError) as exc:
                return ActionResult(outcome="unknown", request_id=action_id,
                                    detail=f"lost contact while polling ({type(exc).__name__}); outcome unknown, not retried")
            if res.get("terminal"):
                result = res.get("result") or {}
                status = res["status"]
                outcome = {"succeeded": "success", "simulated": "success", "failed": "failed", "blocked": "failed"}.get(status, "unknown")
                return ActionResult(outcome=outcome, request_id=action_id, response=res, simulated=status == "simulated",
                                    refused=status == "blocked", detail=result.get("detail", status))
            if waited >= self.poll_timeout_s:
                return ActionResult(outcome="unknown", request_id=action_id, response=res,
                                    detail=f"still {res.get('status')} after {self.poll_timeout_s:.0f}s; outcome unknown")
            self.sleep(self.poll_interval_s)
            waited += self.poll_interval_s

    def record_verification(self, action_id: str, report: VerificationReport) -> None:
        self._call("POST", f"/v1/actions/{action_id}/verification", report)

    def system_state(self) -> dict:
        return self._call("GET", "/v1/services")[1]

    def list_actions(self) -> dict:
        return self._call("GET", "/v1/actions")[1]
