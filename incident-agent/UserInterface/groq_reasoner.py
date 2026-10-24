"""Groq-powered Reasoner for Incident Zero (OpenAI function-calling spec)."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, List, Optional
import httpx
from pydantic import BaseModel, Field

from contracts import (
    AgentDecision, CallToolDecision, FinishDecision, InconclusiveDecision, MalformedDecision,
    ModelError, ReasoningContext, ToolCall, ToolOutcome,
)
from models import ConfidenceLabel, Diagnosis
import reasoning

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "qwen/qwen3.8-27b"

class GroqReasoner:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: Optional[str] = None, timeout_s: float = 30.0):
        self.model = model
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.timeout_s = timeout_s
        self.messages: list[dict[str, Any]] = []
        self._pending_ids: list[str] = []

    def next_step(self, ctx: ReasoningContext) -> AgentDecision:
        if not self.api_key:
            raise ModelError("GROQ_API_KEY is not configured")

        # Format OpenAI tool specs
        tools: list[dict[str, Any]] = []
        if ctx.allow_tools:
            for spec in ctx.tool_specs:
                tools.append(spec)

        # Terminal tools
        tools.append({
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Submit final diagnosis with cited evidence IDs",
                "parameters": Diagnosis.model_json_schema()
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "inconclusive",
                "description": "Stop without diagnosis: evidence insufficient",
                "parameters": reasoning._Inconclusive.model_json_schema()
            }
        })

        # Append pending outcomes as tool messages if any
        if ctx.pending_outcomes:
            for outcome in ctx.pending_outcomes:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": outcome.call_id,
                    "content": f"[TELEMETRY EVIDENCE]\n{outcome.text}"
                })
            self._pending_ids = []

        # User prompt / turn status
        status_text = (
            f"Budget left: {ctx.remaining_tool_calls} tool calls, {ctx.remaining_model_calls} model requests, "
            f"{ctx.remaining_seconds:.0f}s." + ("" if ctx.allow_tools else " No more telemetry tools: call finish or inconclusive now.")
        )

        if not self.messages:
            inc_data = ctx.incident.model_dump(mode="json")
            user_msg = f"Investigate this incident.\nIncident: {json.dumps(inc_data)}\n{ctx.live_only_note}\n{status_text}"
            self.messages = [
                {"role": "system", "content": reasoning.SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}
            ]
        else:
            self.messages.append({"role": "user", "content": status_text})

        payload = {
            "model": self.model,
            "messages": self.messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.0,
            "max_tokens": 1500
        }

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        try:
            timeout = max(2.0, min(ctx.request_timeout_s, ctx.remaining_seconds))
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(f"{GROQ_BASE_URL}/chat/completions", headers=headers, json=payload)
            if resp.status_code != 200:
                raise ModelError(f"Groq API returned HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            choice = data["choices"][0]["message"]
        except Exception as exc:
            raise ModelError(f"Groq request failed: {exc}")

        tool_calls = choice.get("tool_calls") or []
        usage = data.get("usage", {})
        u_dict = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0)
        }

        # Save assistant message into history
        self.messages.append(choice)

        if not tool_calls:
            content = choice.get("content") or ""
            return MalformedDecision(detail=f"No tool called. Text: {content[:150]}", **u_dict)

        # Check for terminal tools
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                fn_args = {}

            if fn_name == "finish":
                try:
                    diag = Diagnosis.model_validate(fn_args)
                    return FinishDecision(diagnosis=diag, **u_dict)
                except Exception as err:
                    return MalformedDecision(detail=f"Invalid finish arguments: {err}", **u_dict)
            elif fn_name == "inconclusive":
                reason = fn_args.get("reason", "inconclusive")
                rel = fn_args.get("relevant_evidence_ids", [])
                return InconclusiveDecision(reason=reason, relevant_evidence_ids=rel, **u_dict)

        # Telemetry tool calls
        calls = []
        for tc in tool_calls:
            cid = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                fn_args = {}
            calls.append(ToolCall(call_id=cid, tool_name=fn_name, arguments=fn_args))

        self._pending_ids = [c.call_id for c in calls]
        return CallToolDecision(calls=calls, **u_dict)
