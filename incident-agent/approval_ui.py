"""Browser approval page served by the Action API (`/ui`). Plain HTML, no scripts; every value is escaped.

The page shows the exact ActionPayload the agent asked to run. Approving needs the one-time code printed only on the
Action API console, so neither the agent nor anything else holding the bearer token can approve its own request.
"""
from __future__ import annotations

import json
from html import escape
from typing import Any, Optional

CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
_STATUS_COLOR = {"pending": "#b26a00", "approved": "#1a7f37", "consumed": "#1a7f37", "rejected": "#a40e26",
                 "expired": "#6e7781", "locked": "#a40e26"}
_STYLE = """
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f6f8fa;color:#1f2328}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}
.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:14px 16px;margin:12px 0}
.badge{display:inline-block;color:#fff;border-radius:12px;padding:1px 10px;font-size:13px;font-weight:600}
pre{background:#0d1117;color:#e6edf3;padding:12px;border-radius:6px;overflow-x:auto;font-size:13px;white-space:pre-wrap;
word-break:break-word}
table{border-collapse:collapse;width:100%}td{padding:4px 8px;border-bottom:1px solid #eaeef2;vertical-align:top}
td:first-child{color:#57606a;width:160px}
input[type=text]{font:16px ui-monospace,monospace;padding:6px 8px;border:1px solid #d0d7de;border-radius:6px;width:180px}
button{font-size:15px;padding:7px 18px;border-radius:6px;border:1px solid #d0d7de;cursor:pointer;margin-right:8px}
.approve{background:#1a7f37;color:#fff;border-color:#1a7f37}.reject{background:#fff;color:#a40e26}
.msg{padding:10px 14px;border-radius:6px;background:#fff8c5;border:1px solid #d4a72c}
.muted{color:#57606a;font-size:13px}a{color:#0969da}
"""


def _page(title: str, body: str, refresh: Optional[int] = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body><main>{body}</main></body></html>")


def _badge(status: str) -> str:
    return f'<span class="badge" style="background:{_STATUS_COLOR.get(status, "#6e7781")}">{escape(status)}</span>'


def message_page(title: str, text: str) -> str:
    return _page(title, f"<h1>{escape(title)}</h1><p>{escape(text)}</p>")


def index_page(rows: list[dict[str, Any]]) -> str:
    items = "".join(
        f'<tr><td>{_badge(r["status"])}</td><td><a href="/ui/approvals/{escape(r["approval_id"])}">'
        f'{escape(r["approval_id"])}</a> · {escape(r["incident_id"])} · <b>{escape(r["operation"])}</b> → '
        f'<b>{escape(r["target"])}</b><div class="muted">created {escape(r["created_at"][:19])} UTC</div></td></tr>'
        for r in rows) or '<tr><td colspan="2">No approval requests yet. This page refreshes every 5 seconds.</td></tr>'
    return _page("Action approvals", f"<h1>Action approvals</h1><p class='muted'>Newest first. Pending requests wait for "
                 f"a human decision; this page refreshes every 5 seconds.</p><div class='card'><table>{items}</table></div>",
                 refresh=5)


def approval_page(rec: dict[str, Any], message: str = "", grafana_links: Optional[list[tuple[str, str]]] = None) -> str:
    """rec: the approval view (status, ids, expiry, approved_by) plus 'payload' and 'form_token'."""
    status, payload = rec["status"], rec.get("payload") or {}
    body = [f'<p class="muted"><a href="/ui">All approvals</a></p>',
            f"<h1>Approve this action? {_badge(status)}</h1>"]
    if message:
        body.append(f'<p class="msg">{escape(message)}</p>')
    rows = [("Incident", rec["incident_id"]), ("Operation", rec["operation"]), ("Target", rec["target"]),
            ("Arguments", json.dumps(payload.get("arguments", {}), sort_keys=True)),
            ("Payload digest", rec["payload_digest"]), ("Requested by", rec["requested_by"]),
            ("Expires (UTC)", str(rec["expires_at"])[:19])]
    if rec.get("approved_by"):
        rows.append(("Approved by", rec["approved_by"]))
    body.append('<div class="card"><b>Exact action (authoritative)</b><table>'
                + "".join(f"<tr><td>{escape(k)}</td><td><code>{escape(str(v))}</code></td></tr>" for k, v in rows)
                + "</table></div>")
    body.append('<div class="card"><b>Action API request</b> <span class="muted">(team-defined ActionPayload; '
                'the reason is written by the model and is untrusted)</span>'
                f"<pre>{escape(json.dumps(payload, indent=2, ensure_ascii=True))}</pre></div>")
    if grafana_links:
        body.append('<div class="card"><b>Evidence in Grafana</b><ul>' + "".join(
            f'<li><a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a></li>'
            for label, url in grafana_links) + "</ul></div>")
    if status == "pending":
        aid = escape(rec["approval_id"], quote=True)
        body.append(
            f'<form class="card" method="post" action="/ui/approvals/{aid}" autocomplete="off">'
            f'<input type="hidden" name="form_token" value="{escape(rec.get("form_token", ""), quote=True)}">'
            "<p><b>To approve</b>, type the <b>ONE-TIME APPROVAL CODE</b> printed for this request in the Action API "
            "terminal. Rejecting needs no code.</p>"
            '<p><label>Your name <input type="text" name="approver" maxlength="80" required></label></p>'
            '<p><label>Code <input type="text" name="code" maxlength="9" placeholder="XXXX-XXXX" '
            'pattern="[A-Za-z2-9]{4}-?[A-Za-z2-9]{4}"></label></p>'
            '<button class="approve" type="submit" name="decision" value="approve">Approve and run</button>'
            '<button class="reject" type="submit" name="decision" value="reject" formnovalidate>Reject</button>'
            "</form>")
    else:
        body.append(f'<p class="muted">This request is {escape(status)}; no further decision is possible here.</p>')
    return _page(f"Approval {rec['approval_id']}", "".join(body), refresh=None if status == "pending" else 10)
