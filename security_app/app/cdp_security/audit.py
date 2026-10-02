"""Append-only audit trail. Rows are hash-chained by a database trigger.

Always call `record` with the same cursor as the change it describes, so the
audit row commits or rolls back together with the change.
"""
from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb

from .models import CurrentUser

# Never written to the audit trail even if present in a row snapshot
_REDACT = {"password", "token", "secret", "client_secret"}


def _clean(state: Any) -> Any:
    if state is None:
        return None
    if isinstance(state, dict):
        return {k: ("***" if k.lower() in _REDACT else _clean(v)) for k, v in state.items()}
    if isinstance(state, (list, tuple)):
        return [_clean(v) for v in state]
    return state


def _jsonb(state: Any) -> Jsonb | None:
    if state is None:
        return None
    return Jsonb(_clean(state), dumps=lambda o: json.dumps(o, default=str, sort_keys=True))


def record(cur, actor: CurrentUser | str, action: str, object_kind: str, object_id: Any = None, *,
           before: Any = None, after: Any = None, reason: str | None = None,
           correlation_id: Any = None) -> None:
    if isinstance(actor, CurrentUser):
        actor_name, actor_type = actor.email, actor.actor_type
    else:
        actor_name = str(actor)
        actor_type = "SYSTEM" if actor_name.startswith("system:") else "USER"
    cur.execute(
        """
        INSERT INTO audit_event (actor, actor_type, action, object_kind, object_id, correlation_id,
                                 before_state, after_state, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (actor_name, actor_type, action, object_kind,
         None if object_id is None else str(object_id),
         None if correlation_id is None else str(correlation_id),
         _jsonb(before), _jsonb(after), reason),
    )
