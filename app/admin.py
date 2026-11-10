"""Admin dashboard: call history with transcripts and tool calls, tickets, callbacks.

HTTP Basic auth with ADMIN_USER / ADMIN_PASSWORD. With no ADMIN_PASSWORD the routes return 404.
HTML pages for people, /admin/api/* JSON for integrations.
"""

import json
import secrets
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import calls, db
from .config import get_settings

router = APIRouter(prefix="/admin")
_basic = HTTPBasic(auto_error=False)

OUTCOME_LABELS = {
    "completed": "Completed",
    "caller_hung_up": "Caller hung up",
    "transferred": "Transferred",
    "no_response": "No response",
    "rejected_busy": "Busy (rejected)",
    "max_duration": "Time limit",
    "server_shutdown": "Server restart",
    "error_speech": "Speech service down",
    None: "In progress",
}


def require_admin(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    s = get_settings()
    if not s.admin_password:
        raise HTTPException(status_code=404)
    ok = creds is not None and secrets.compare_digest(creds.username, s.admin_user) and secrets.compare_digest(
        creds.password, s.admin_password
    )
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": 'Basic realm="ringmind admin"'})


# -- queries -----------------------------------------------------------------------------------------


async def _calls(limit: int, outcome: str | None) -> list[dict]:
    async with db.pool().acquire() as c:
        rows = await c.fetch(
            """SELECT k.id, k.transport, k.caller_phone, cu.full_name AS customer, k.started_at, k.ended_at,
                      EXTRACT(EPOCH FROM (COALESCE(k.ended_at, now()) - k.started_at))::int AS seconds,
                      k.outcome, k.verified, k.turns, k.avg_response_ms, k.models_used
               FROM calls k LEFT JOIN customers cu ON cu.id = k.customer_id
               WHERE $2::text IS NULL OR k.outcome = $2
               ORDER BY k.started_at DESC LIMIT $1""",
            limit,
            outcome,
        )
    return [dict(r) for r in rows]


async def _call(call_id: str) -> tuple[dict | None, list[dict]]:
    async with db.pool().acquire() as c:
        call = await c.fetchrow(
            """SELECT k.*, cu.full_name AS customer FROM calls k LEFT JOIN customers cu ON cu.id = k.customer_id
               WHERE k.id = $1""",
            call_id,
        )
        events = await c.fetch("SELECT at, kind, text, data FROM call_events WHERE call_id = $1 ORDER BY id", call_id)
    return (dict(call) if call else None), [dict(e) for e in events]


async def _stats() -> dict:
    async with db.pool().acquire() as c:
        row = await c.fetchrow(
            """SELECT count(*) AS calls_24h,
                      count(*) FILTER (WHERE outcome = 'completed') AS completed_24h,
                      count(*) FILTER (WHERE outcome = 'transferred') AS transferred_24h,
                      round(avg(avg_response_ms))::int AS avg_response_ms_24h
               FROM calls WHERE started_at > now() - interval '24 hours'"""
        )
        open_tickets = await c.fetchval("SELECT count(*) FROM tickets WHERE status = 'open'")
        pending_callbacks = await c.fetchval("SELECT count(*) FROM callbacks WHERE status = 'pending'")
    return {**dict(row), "active_now": calls.active_count(), "open_tickets": open_tickets, "pending_callbacks": pending_callbacks}


# -- JSON API -------------------------------------------------------------------------------------


@router.get("/api/stats", dependencies=[Depends(require_admin)])
async def api_stats() -> dict:
    return await _stats()


@router.get("/api/calls", dependencies=[Depends(require_admin)])
async def api_calls(limit: int = Query(50, ge=1, le=500), outcome: str | None = None) -> list[dict]:
    return await _calls(limit, outcome)


@router.get("/api/calls/{call_id}", dependencies=[Depends(require_admin)])
async def api_call(call_id: str) -> dict:
    call, events = await _call(call_id)
    if call is None:
        raise HTTPException(status_code=404)
    for e in events:
        e["data"] = json.loads(e["data"]) if e["data"] else None
    return {"call": call, "events": events}


# -- HTML ------------------------------------------------------------------------------------------

_STYLE = """
:root{--bg:#f7f7f8;--card:#fff;--fg:#1c1c1f;--muted:#6b6b76;--line:#e4e4e8;--accent:#3b5bdb;
--ok:#2b8a3e;--warn:#c77700;--bad:#c92a2a;--bot:#eef2ff;--caller:#f1f3f5;--tool:#fff8e6}
@media (prefers-color-scheme:dark){:root{--bg:#141417;--card:#1d1d21;--fg:#ececf1;--muted:#9a9aa6;
--line:#2e2e35;--accent:#8ea2ff;--ok:#69db7c;--warn:#ffc078;--bad:#ff8787;--bot:#22264a;--caller:#26262c;--tool:#33291a}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:16px}nav a{margin-right:14px;color:var(--accent);text-decoration:none}
main{max-width:1100px;margin:0 auto;padding:16px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.stat span{display:block;color:var(--muted);font-size:12px}.stat strong{font-size:20px}
.table{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}td.wrap{white-space:normal;min-width:240px}
a{color:var(--accent)}.muted{color:var(--muted)}.pill{padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.ev{margin:6px 0;padding:8px 10px;border-radius:8px;max-width:760px}.ev small{color:var(--muted);display:block;font-size:11px}
.caller{background:var(--caller)}.bot{background:var(--bot);margin-left:40px}.tool{background:var(--tool);font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap;margin-left:40px}
.system,.metric{color:var(--muted);font-size:12px;padding:2px 10px}
"""


def _page(title: str, body: str) -> HTMLResponse:
    s = get_settings()
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{_STYLE}</style></head><body>
<header><b>{escape(s.bot_name)} · {escape(s.company_name)}</b><nav><a href="/admin">Calls</a><a href="/admin/tickets">Tickets</a>
<a href="/admin/callbacks">Callbacks</a><a href="/talk">Test call</a></nav></header><main>{body}</main></body></html>"""
    )


def _outcome(o: str | None) -> str:
    cls = {"completed": "ok", "transferred": "ok", None: "muted"}.get(o, "warn" if o in ("caller_hung_up", "no_response") else "bad")
    return f'<span class="pill {cls}">{escape(OUTCOME_LABELS.get(o, o or ""))}</span>'


def _mask(phone: str | None) -> str:
    return escape(phone[:-4] + "••••" if phone and len(phone) > 6 else phone or "")


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def calls_page(outcome: str | None = None) -> HTMLResponse:
    st = await _stats()
    stats = "".join(
        f'<div class="stat"><span>{label}</span><strong>{"—" if v is None else v}</strong></div>'
        for label, v in [
            ("Active now", st["active_now"]),
            ("Calls, 24 h", st["calls_24h"]),
            ("Completed, 24 h", st["completed_24h"]),
            ("Transferred, 24 h", st["transferred_24h"]),
            ("Avg response, ms", st["avg_response_ms_24h"]),
            ("Open tickets", st["open_tickets"]),
            ("Pending callbacks", st["pending_callbacks"]),
        ]
    )
    rows = "".join(
        f"""<tr><td><a href="/admin/calls/{escape(c['id'])}">{c['started_at']:%d %b %H:%M}</a></td>
<td>{escape(c['customer'] or 'Unknown')}<br><span class="muted">{_mask(c['caller_phone'])}</span></td>
<td>{escape(c['transport'])}</td><td>{c['seconds'] // 60}:{c['seconds'] % 60:02d}</td><td>{_outcome(c['outcome'])}</td>
<td>{c['turns']}</td><td>{'yes' if c['verified'] else '—'}</td><td>{c['avg_response_ms'] or '—'}</td>
<td class="muted">{escape(', '.join(m.split('/', 1)[-1] for m in c['models_used']))}</td></tr>"""
        for c in await _calls(200, outcome)
    )
    return _page(
        "Calls",
        f"""<div class="stats">{stats}</div><div class="table"><table><tr><th>Started</th><th>Caller</th><th>Via</th>
<th>Length</th><th>Outcome</th><th>Turns</th><th>Verified</th><th>Avg response ms</th><th>Models</th></tr>
{rows or '<tr><td colspan="9" class="muted">No calls yet.</td></tr>'}</table></div>""",
    )


@router.get("/calls/{call_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def call_page(call_id: str) -> HTMLResponse:
    call, events = await _call(call_id)
    if call is None:
        raise HTTPException(status_code=404)
    parts = []
    for e in events:
        at = f"<small>{e['at']:%H:%M:%S}</small>"
        data = json.loads(e["data"]) if e["data"] else {}
        if e["kind"] in ("caller", "bot"):
            who = "Caller" if e["kind"] == "caller" else get_settings().bot_name
            parts.append(f'<div class="ev {e["kind"]}">{at}<b>{escape(who)}:</b> {escape(e["text"] or "")}</div>')
        elif e["kind"] == "tool":
            body = f'{e["text"]}({json.dumps(data.get("args"))})\n→ {json.dumps(data.get("result"))[:1500]}'
            parts.append(f'<div class="ev tool">{at}{escape(body)}</div>')
        elif e["kind"] == "metric":
            parts.append(f'<div class="ev metric">response in {data.get("ms")} ms</div>')
        else:
            parts.append(f'<div class="ev system">{at}{escape(e["text"] or "")}</div>')
    meta = (
        f"{escape(call['customer'] or 'Unknown caller')} · {_mask(call['caller_phone'])} · {escape(call['transport'])} · "
        f"{call['started_at']:%d %b %Y %H:%M} · {_outcome(call['outcome'])} · verified: {'yes' if call['verified'] else 'no'}"
    )
    return _page(f"Call {call_id}", f"<p>{meta}</p>{''.join(parts) or '<p class=muted>No events.</p>'}")


async def _simple_table(sql: str, headers: list[str], keys: list[str], empty: str) -> str:
    async with db.pool().acquire() as c:
        rows = await c.fetch(sql)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{"wrap" if k in ("description", "reason") else ""}">'
            + (
                f'<a href="/admin/calls/{escape(str(r[k]))}">view</a>'
                if k == "call_id" and r[k]
                else escape(f"{r[k]:%d %b %H:%M}" if hasattr(r[k], "strftime") else str(r[k] if r[k] is not None else "—"))
            )
            + "</td>"
            for k in keys
        )
        + "</tr>"
        for r in rows
    )
    head = "".join(f"<th>{h}</th>" for h in headers)
    return f'<div class="table"><table><tr>{head}</tr>{body or f"<tr><td class=muted colspan={len(keys)}>{empty}</td></tr>"}</table></div>'


@router.get("/tickets", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def tickets_page() -> HTMLResponse:
    table = await _simple_table(
        """SELECT t.id, t.created_at, COALESCE(cu.full_name, t.caller_phone) AS who, t.priority, t.status,
                  t.subject, t.description, t.call_id
           FROM tickets t LEFT JOIN customers cu ON cu.id = t.customer_id ORDER BY t.created_at DESC LIMIT 200""",
        ["#", "Created", "Customer", "Priority", "Status", "Subject", "Description", "Call"],
        ["id", "created_at", "who", "priority", "status", "subject", "description", "call_id"],
        "No tickets yet.",
    )
    return _page("Tickets", table)


@router.get("/callbacks", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def callbacks_page() -> HTMLResponse:
    table = await _simple_table(
        """SELECT b.id, b.created_at, COALESCE(cu.full_name, 'Unknown') AS who, b.phone, b.preferred_time,
                  b.reason, b.status, b.call_id
           FROM callbacks b LEFT JOIN customers cu ON cu.id = b.customer_id ORDER BY b.created_at DESC LIMIT 200""",
        ["#", "Requested", "Customer", "Phone", "When", "Reason", "Status", "Call"],
        ["id", "created_at", "who", "phone", "preferred_time", "reason", "status", "call_id"],
        "No callbacks yet.",
    )
    return _page("Callbacks", table)
