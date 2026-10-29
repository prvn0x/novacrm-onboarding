"""SQLite storage: the deals, the Gmail messages already handled, and the audit log (design D9).

A deal is saved after every step, so a crash resumes from the last saved state.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from .domain import TERMINAL_STATES, Deal, DealNotification, DealState, ReviewReason, Tier

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    opportunity_id TEXT,
    state TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deals_thread ON deals (thread_id);
CREATE INDEX IF NOT EXISTS deals_opportunity ON deals (opportunity_id);
CREATE TABLE IF NOT EXISTS messages (message_id TEXT PRIMARY KEY, deal_key TEXT);
CREATE TABLE IF NOT EXISTS threads (thread_id TEXT PRIMARY KEY, deal_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    deal TEXT NOT NULL,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    input TEXT NOT NULL,
    output TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_deal ON audit (deal, seq);
"""

_DATETIME_FIELDS = {
    "state_since",
    "received_at",
    "next_call_at",
    "call_started_at",
    "pending_since",
    "next_retry_at",
}


def _to_json(deal: Deal) -> str:
    data = asdict(deal)
    for name in _DATETIME_FIELDS:
        value = getattr(deal, name)
        data[name] = value.isoformat() if value else None
    data["notification"] = deal.notification.model_dump() if deal.notification else None
    return json.dumps(data)


def _from_json(raw: str) -> Deal:
    data: dict[str, Any] = json.loads(raw)
    known = {f.name for f in fields(Deal)}
    data = {k: v for k, v in data.items() if k in known}
    for name in _DATETIME_FIELDS:
        if data.get(name):
            data[name] = datetime.fromisoformat(data[name])
    data["state"] = DealState(data["state"])
    data["tier"] = Tier(data["tier"]) if data.get("tier") else None
    data["review_reason"] = (
        ReviewReason(data["review_reason"]) if data.get("review_reason") else None
    )
    data["resume_state"] = DealState(data["resume_state"]) if data.get("resume_state") else None
    if data.get("notification"):
        data["notification"] = DealNotification(**data["notification"])
    return Deal(**data)


# Masking for the audit log: phone numbers keep their last 4 digits, emails their first letter.
# A phone is "+" then digits (E.164 as stored), or a bare 10-12 digit run. Dates, Slack message
# timestamps (1793181600.000100) and epoch milliseconds (13 digits) do not match.
_PHONE = re.compile(r"\+\d[\d \-()]{6,}\d|(?<![\w.-])\d{10,12}(?![\w.-])")
_EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def mask(text: str) -> str:
    text = _PHONE.sub(lambda m: "******" + re.sub(r"\D", "", m.group(0))[-4:], text)
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)


def _masked_json(value: Any) -> str:
    return mask(json.dumps(value, default=str, ensure_ascii=False))


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    # deals
    def new_key(self) -> str:
        """Short, human-friendly deal keys for the CLI: D1, D2, ..."""
        # Not MAX(seq): every upsert of a deal advances AUTOINCREMENT (seen live: D1, D2, D16).
        row = self._db.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(key, 2) AS INTEGER)), 0) + 1 AS n FROM deals"
        ).fetchone()
        return f"D{row['n']}"

    def save(self, deal: Deal) -> None:
        opportunity = (
            deal.notification.opportunity_id
            if deal.notification
            else deal.partial_opportunity or None
        )
        with self._db:
            self._db.execute(
                "INSERT INTO deals (key, thread_id, opportunity_id, state, data) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                "thread_id = excluded.thread_id, opportunity_id = excluded.opportunity_id, "
                "state = excluded.state, data = excluded.data",
                (deal.key, deal.thread_id, opportunity, deal.state.value, _to_json(deal)),
            )

    def get(self, key: str) -> Deal | None:
        row = self._db.execute("SELECT data FROM deals WHERE key = ?", (key,)).fetchone()
        return _from_json(row["data"]) if row else None

    def by_thread(self, thread_id: str) -> Deal | None:
        """The deal a thread belongs to (a deal can absorb another deal's thread, D5)."""
        row = self._db.execute(
            "SELECT d.data FROM threads t JOIN deals d ON d.key = t.deal_key WHERE t.thread_id = ?",
            (thread_id,),
        ).fetchone()
        return _from_json(row["data"]) if row else None

    def attach_thread(self, thread_id: str, deal_key: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO threads (thread_id, deal_key) VALUES (?, ?) "
                "ON CONFLICT (thread_id) DO UPDATE SET deal_key = excluded.deal_key",
                (thread_id, deal_key),
            )

    def by_opportunity(self, opportunity_id: str) -> list[Deal]:
        rows = self._db.execute(
            "SELECT data FROM deals WHERE opportunity_id = ? ORDER BY seq", (opportunity_id,)
        ).fetchall()
        return [_from_json(r["data"]) for r in rows]

    def open_deals(self) -> list[Deal]:
        placeholders = ",".join("?" * len(TERMINAL_STATES))
        rows = self._db.execute(
            f"SELECT data FROM deals WHERE state NOT IN ({placeholders}) ORDER BY seq",
            [s.value for s in TERMINAL_STATES],
        ).fetchall()
        return [_from_json(r["data"]) for r in rows]

    def all_deals(self) -> list[Deal]:
        rows = self._db.execute("SELECT data FROM deals ORDER BY seq").fetchall()
        return [_from_json(r["data"]) for r in rows]

    # messages
    def seen(self, message_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def remember(self, message_id: str, deal_key: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO messages (message_id, deal_key) VALUES (?, ?)",
                (message_id, deal_key),
            )

    # audit
    def audit(
        self,
        ts: datetime,
        deal: str,
        agent: str,
        action: str,
        *,
        input: Any = None,
        output: Any = None,
        decision: str = "",
        reason: str = "",
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO audit (ts, deal, agent, action, input, output, decision, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts.isoformat(timespec="seconds"),
                    deal,
                    agent,
                    action,
                    _masked_json(input),
                    _masked_json(output),
                    decision,
                    mask(reason),
                ),
            )

    def trail(self, deal: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit" + (" WHERE deal = ?" if deal else "") + " ORDER BY seq"
        rows = self._db.execute(sql, (deal,) if deal else ()).fetchall()
        return [
            {**dict(r), "input": json.loads(r["input"]), "output": json.loads(r["output"])}
            for r in rows
        ]
