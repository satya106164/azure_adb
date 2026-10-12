"""Append-only, hash-chained audit trail.

Each event is one INSERT ... SELECT that reads the last event and computes the
next chain_seq and the SHA-256 hash in the same statement. audit_event uses
Serializable isolation, so two writers racing for the same chain_seq conflict
and the loser is retried by db.py; the chain stays strictly linear.
The table is append-only (delta.appendOnly): UPDATE and DELETE are refused.

Delta has no multi-table transactions, so record the event straight after the
change it describes. If the change itself failed, nothing is recorded.
"""
from __future__ import annotations

import json
from typing import Any

from . import db
from .models import CurrentUser

# Never written to the audit trail even if present in a row snapshot
_REDACT = {"password", "token", "secret", "client_secret"}

# One expression used to write and to verify, so the two can never disagree.
_HASH = ("sha2(concat_ws('|', CAST({seq} AS STRING), {prev}, "
         "date_format({at}, \"yyyy-MM-dd'T'HH:mm:ss.SSSSSS\"), {actor}, {actor_type}, {action}, {kind}, "
         "coalesce({obj}, ''), coalesce({corr}, ''), coalesce({before}, ''), coalesce({after}, ''), "
         "coalesce({reason}, '')), 256)")

_INSERT = f"""
INSERT INTO audit_event (chain_seq, occurred_at, actor, actor_type, action, object_kind, object_id, correlation_id,
                         before_state, after_state, reason, prev_hash, event_hash)
SELECT s.seq, :at, :actor, :actor_type, :action, :kind, :obj, :corr, :before, :after, :reason, s.prev,
       {_HASH.format(seq="s.seq", prev="s.prev", at=":at", actor=":actor", actor_type=":actor_type",
                     action=":action", kind=":kind", obj=":obj", corr=":corr", before=":before", after=":after",
                     reason=":reason")}
FROM (SELECT coalesce(max(chain_seq), 0) + 1 AS seq,
             coalesce(max_by(event_hash, chain_seq), 'GENESIS') AS prev
      FROM audit_event) s
"""

_VERIFY = f"""
WITH o AS (
  SELECT chain_seq, prev_hash, event_hash,
         lag(event_hash) OVER (ORDER BY chain_seq) AS lag_hash,
         lag(chain_seq) OVER (ORDER BY chain_seq) AS lag_seq,
         {_HASH.format(seq="chain_seq", prev="prev_hash", at="occurred_at", actor="actor", actor_type="actor_type",
                       action="action", kind="object_kind", obj="object_id", corr="correlation_id",
                       before="before_state", after="after_state", reason="reason")} AS calc
  FROM audit_event)
SELECT chain_seq AS broken_at_seq,
       CASE WHEN event_hash <> calc THEN 'event hash does not match content'
            WHEN prev_hash <> coalesce(lag_hash, 'GENESIS') THEN 'prev_hash does not match previous event'
            ELSE 'sequence gap or duplicate' END AS problem
FROM o
WHERE event_hash <> calc OR prev_hash <> coalesce(lag_hash, 'GENESIS') OR chain_seq <> coalesce(lag_seq, 0) + 1
ORDER BY chain_seq
LIMIT 100
"""


def _clean(state: Any) -> Any:
    if state is None:
        return None
    if isinstance(state, dict):
        return {k: ("***" if k.lower() in _REDACT else _clean(v)) for k, v in state.items()}
    if isinstance(state, (list, tuple)):
        return [_clean(v) for v in state]
    return state


def _json(state: Any) -> str | None:
    if state is None:
        return None
    return json.dumps(_clean(state), default=str, sort_keys=True)


def record(actor: CurrentUser | str, action: str, object_kind: str, object_id: Any = None, *,
           before: Any = None, after: Any = None, reason: str | None = None, correlation_id: Any = None) -> None:
    if isinstance(actor, CurrentUser):
        actor_name, actor_type = actor.email, actor.actor_type
    else:
        actor_name = str(actor)
        actor_type = "SYSTEM" if actor_name.startswith("system:") else "USER"
    db.execute(_INSERT, {
        "at": db.now(), "actor": actor_name, "actor_type": actor_type, "action": action, "kind": object_kind,
        "obj": None if object_id is None else str(object_id),
        "corr": None if correlation_id is None else str(correlation_id),
        "before": _json(before), "after": _json(after), "reason": reason,
    })


def verify_chain() -> list[dict]:
    """Rows only where the chain is broken (empty result = intact)."""
    return db.fetch_all(_VERIFY)
