# Action API

There are two ways the agent can execute an approved remediation:

1. **Team Action API (implemented, recommended for the demo).** This is our own minimal, secure API:
   - `action_server.py`, `action_executor.py`, `action_api_client.py`
   - contract: `docs/action_api.openapi.json` and `schemas/action_api/`
   - design: [docs/ACTION_API_ARCHITECTURE.md](docs/ACTION_API_ARCHITECTURE.md)
   - demo: [docs/END_TO_END_DEMO.md](docs/END_TO_END_DEMO.md)

   Use it with `python cli.py action-api serve` and `investigate ... --respond --action-api [--execute]`. **It is
   team-defined, not an organiser contract**, and makes no claim to satisfy an organiser-specific requirement.
2. **Organiser Action API (catalog adapter, waiting for a contract).** No organiser contract has been supplied. None
   of the local repository, the team branches, the compose files, the env files or the running containers contain an
   endpoint, schema or auth scheme. If one is supplied, describe it in a catalog file (format below) and use
   `--action-catalog <file> --execute`. `tests/fixtures/mock_action_catalog.json` (`"provenance": "mock"`) is for
   tests only, and `HttpActionClient` refuses it for live dispatch.

## Questions for mentors (still open for an official API)

1. Base URL, and whether it is reachable from the host or only inside the Compose network.
2. Authentication scheme (header and token source).
3. Operation catalogue: names, allowed targets, parameters (types and ranges).
4. Request and response schema per operation, including the request-ID field.
5. Status codes: rejected vs failed vs accepted-async.
6. Idempotency-key semantics; status or reconciliation endpoint for unknown outcomes.
7. Dry-run support, and whether it guarantees no mutation.
8. Rollback operations.
9. Timeouts, rate limits and per-incident action limits.
10. A read-only discovery endpoint (OpenAPI or `GET /actions`).
11. The official incident-report JSON schema.

## Catalog format (`ACTION_API_CATALOG`)

Write this from the official contract. Validation is fail-closed: unknown schema keywords, open-ended targets and
duplicate names are rejected.

```json
{
  "provenance": "organiser",
  "base_url_env": "ACTION_API_URL",
  "auth_header": "Authorization", "auth_scheme": "Bearer", "token_env": "ACTION_API_TOKEN",
  "timeout_s": 15,
  "operations": [{
    "name": "restart_service", "description": "...", "method": "POST", "path": "/actions/restart",
    "target_kind": "service", "target_field": "service", "allowed_targets": ["cart", "payment"],
    "parameters": {"grace_period_seconds": {"type": "integer", "minimum": 0, "maximum": 60, "default": 10}},
    "required": [], "risk": "...", "reversible": false, "rollback": null, "idempotent": false,
    "reduces_load": false, "expected_effect": "..."
  }]
}
```

Sent request: `{method} {ACTION_API_URL}{path}`. The JSON body is the validated arguments plus
`{target_field: target}`, sent with an `Idempotency-Key` header. Response handling: 2xx is success; 4xx is failed
(not retried); 5xx, a redirect or a timeout after sending is unknown (never retried, escalated, and verified from
telemetry only). If the real request or response shape differs, change only `actions.build_request` and
`HttpActionClient.execute`.

## Enforced by the agent, not assumed from the API

These checks live in `safety.py` and `response.py`, in order:

1. Allowlist and strict arguments.
2. The target must be approved, in the observed inventory, named by the diagnosis and mentioned in cited ok evidence.
3. The baseline is captured before approval, and verification needs a symptom present at baseline.
4. Kill switch.
5. Terminal approval: the operator types `approve <code>`, where the code is derived from the SHA-256 of the full
   proposal and the catalog operation. Approval expires after 120s; non-interactive sessions are rejected.
6. Revalidation and fresh symptom check immediately before dispatch.
7. Single dispatch per incident and digest, with an append-only `runs/audit.jsonl` and a dispatch lock.
8. Bounded observation and deterministic verification.
