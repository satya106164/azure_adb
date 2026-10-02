"""Drift detection: compare desired state (Lakebase) with actual state.

Sources of actual state
* Unity Catalog grants   system.information_schema.{catalog,schema,table}_privileges
* Out-of-band changes    system.access.audit (updatePermissions by anyone but the provisioning SP)
* Group membership       Microsoft Graph

Policy encoded here
* Every app-managed group must hold exactly its APPLIED entitlements.
* Nobody may hold a direct (user) grant on a governed securable - access is via groups only.
* On RESTRICTED securables, any grantee not managed by the app or allow-listed is flagged HIGH.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from . import audit, notify
from .config import get_settings
from .db import transaction
from .models import CurrentUser
from .naming import is_managed_group

log = logging.getLogger(__name__)
ACTOR = CurrentUser.system("drift")


@dataclass(frozen=True)
class GrantKey:
    securable_type: str
    securable: str
    principal: str
    privilege: str

    @staticmethod
    def of(securable_type: str, securable: str, principal: str, privilege: str) -> "GrantKey":
        return GrantKey(securable_type.upper(), securable.lower(), principal.lower(), " ".join(privilege.upper().split()))


@dataclass
class Finding:
    finding_type: str
    severity: str
    securable: str | None = None
    principal: str | None = None
    privilege: str | None = None
    details: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return "|".join([self.finding_type, self.securable or "", self.principal or "", self.privilege or ""]).lower()


# ---------------------------------------------------------------------------
# Pure comparison logic (unit tested)
# ---------------------------------------------------------------------------
def compare_grants(desired: set[GrantKey], actual: set[GrantKey], *, env: str,
                   restricted: set[str] = frozenset(), allowlist: Iterable[str] = ()) -> list[Finding]:
    allow = {a.lower() for a in allowlist}
    findings: list[Finding] = []
    for k in sorted(desired - actual, key=str):
        findings.append(Finding("MISSING_GRANT", "MEDIUM", k.securable, k.principal, k.privilege,
                                {"securable_type": k.securable_type}))
    for k in sorted(actual - desired, key=str):
        if k.principal in allow:
            continue
        if "@" in k.principal:
            findings.append(Finding("DIRECT_USER_GRANT", "HIGH", k.securable, k.principal, k.privilege,
                                    {"securable_type": k.securable_type}))
        elif is_managed_group(k.principal, env):
            findings.append(Finding("UNEXPECTED_GRANT", "HIGH" if k.securable in restricted else "MEDIUM",
                                    k.securable, k.principal, k.privilege, {"securable_type": k.securable_type}))
        elif k.securable in restricted:
            findings.append(Finding("UNEXPECTED_GRANT", "HIGH", k.securable, k.principal, k.privilege,
                                    {"securable_type": k.securable_type, "unmanaged_principal": True}))
    return findings


def compare_members(group: str, must_have: set[str], may_have: set[str], actual: set[str],
                    allowlist: Iterable[str] = ()) -> list[Finding]:
    allow = {a.lower() for a in allowlist}
    must = {m.lower() for m in must_have}
    may = {m.lower() for m in may_have} | must
    act = {a.lower() for a in actual}
    out = [Finding("MISSING_MEMBER", "MEDIUM", group, m) for m in sorted(must - act)]
    out += [Finding("UNEXPECTED_MEMBER", "HIGH", group, m) for m in sorted(act - may - allow)]
    return out


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def _desired(cur) -> tuple[set[GrantKey], set[str], set[str], dict[GrantKey, str]]:
    cur.execute("""
        SELECT s.securable_type, s.full_name, g.group_name, e.privilege, e.entitlement_id
        FROM group_entitlement e
        JOIN access_group g ON g.access_group_id = e.access_group_id
        JOIN uc_securable s ON s.securable_id = e.securable_id
        WHERE e.desired_state = 'PRESENT' AND e.apply_status = 'APPLIED' AND g.status IN ('ACTIVE','RETIRING')""")
    ent_ids: dict[GrantKey, str] = {}
    for r in cur.fetchall():
        ent_ids[GrantKey.of(r["securable_type"], r["full_name"], r["group_name"], r["privilege"])] = str(r["entitlement_id"])
    cur.execute("SELECT full_name, securable_type, classification_code FROM uc_securable")
    rows = cur.fetchall()
    catalogs = {r["full_name"].split(".")[0] for r in rows}
    restricted = {r["full_name"] for r in rows if r["classification_code"] == "RESTRICTED"}
    return set(ent_ids), catalogs, restricted, ent_ids


def _actual_uc(spark, catalogs: set[str], governed: set[str]) -> set[GrantKey]:
    out: set[GrantKey] = set()
    for c in sorted(catalogs):
        df = spark.sql("""
            SELECT 'CATALOG' AS t, catalog_name AS securable, grantee, privilege_type
            FROM system.information_schema.catalog_privileges
            WHERE catalog_name = :c AND inherited_from = 'NONE'
            UNION ALL
            SELECT 'SCHEMA', concat(catalog_name, '.', schema_name), grantee, privilege_type
            FROM system.information_schema.schema_privileges
            WHERE catalog_name = :c AND inherited_from = 'NONE'
            UNION ALL
            SELECT 'TABLE', concat_ws('.', table_catalog, table_schema, table_name), grantee, privilege_type
            FROM system.information_schema.table_privileges
            WHERE table_catalog = :c AND inherited_from = 'NONE'
        """, args={"c": c})
        for r in df.collect():
            key = GrantKey.of(r["t"], r["securable"], r["grantee"], r["privilege_type"])
            if key.securable in governed:  # only judge securables registered in the app
                out.add(key)
    return out


def _out_of_band(spark, since: datetime, allowlist: set[str]) -> list[Finding]:
    rows = spark.sql("""
        SELECT event_time, user_identity.email AS actor, action_name, request_params
        FROM system.access.audit
        WHERE service_name = 'unityCatalog' AND action_name = 'updatePermissions' AND event_time >= :since
    """, args={"since": since}).collect()
    out = []
    for r in rows:
        actor = (r["actor"] or "").lower()
        if actor in allowlist:
            continue
        params = r["request_params"] or {}
        securable = f"{params.get('securable_type', '')}:{params.get('securable_full_name', '')}"
        out.append(Finding("OUT_OF_BAND_CHANGE", "HIGH", securable, actor, None,
                           {"event_time": str(r["event_time"]), "changes": params.get("changes")}))
    return out


def _membership(cur, graph, allowlist: set[str]) -> list[Finding]:
    cur.execute("""
        SELECT g.access_group_id, g.group_name, g.entra_object_id,
               array_remove(array_agg(u.email) FILTER (WHERE gr.status = 'ACTIVE'), NULL) AS must,
               array_remove(array_agg(u.email) FILTER (WHERE gr.status IN ('PENDING_PROVISION','PENDING_REVOKE')), NULL) AS may
        FROM access_group g
        LEFT JOIN access_grant gr ON gr.access_group_id = g.access_group_id
        LEFT JOIN app_user u ON u.user_id = gr.user_id
        WHERE g.status IN ('ACTIVE','RETIRING') AND g.entra_object_id NOT LIKE 'dryrun-%'
        GROUP BY g.access_group_id, g.group_name, g.entra_object_id""")
    findings: list[Finding] = []
    for g in cur.fetchall():
        known = {e.lower() for e in (g["must"] or []) + (g["may"] or [])}
        actual: set[str] = set()
        for m in graph.list_user_members(g["entra_object_id"]):
            ids = [(m.get("mail") or "").lower(), (m.get("userPrincipalName") or "").lower()]
            match = next((i for i in ids if i and i in known), None)
            actual.add(match or next((i for i in ids if i), m.get("id", "?")))
        findings += compare_members(g["group_name"], set(g["must"] or []), set(g["may"] or []), actual, allowlist)
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(spark, graph=None, *, provisioning_principal: str, lookback_hours: int = 26,
        remediate_missing: bool = False) -> dict:
    settings = get_settings()
    allow = set(settings.drift_allowlist) | {provisioning_principal.lower()}
    with transaction() as cur:
        cur.execute("INSERT INTO drift_run DEFAULT VALUES RETURNING run_id")
        run_id = cur.fetchone()["run_id"]
        desired, catalogs, restricted, ent_ids = _desired(cur)
        cur.execute("SELECT full_name FROM uc_securable")
        governed = {r["full_name"] for r in cur.fetchall()}
    try:
        actual = _actual_uc(spark, catalogs, governed)
        findings = compare_grants(desired, actual, env=settings.env, restricted=restricted, allowlist=allow)
        findings += _out_of_band(spark, datetime.now(timezone.utc) - timedelta(hours=lookback_hours), allow)
        if graph is not None:
            with transaction() as cur:
                findings += _membership(cur, graph, allow)
    except Exception as exc:
        with transaction() as cur:
            cur.execute("UPDATE drift_run SET status = 'FAILED', finished_at = now(), summary = %s WHERE run_id = %s",
                        (json.dumps({"error": str(exc)[:1000]}), run_id))
        raise

    summary: dict[str, int] = {}
    with transaction() as cur:
        for f in findings:
            summary[f.finding_type] = summary.get(f.finding_type, 0) + 1
            cur.execute("""
                INSERT INTO drift_finding (run_id, finding_type, severity, fingerprint, securable, principal,
                                           privilege, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fingerprint) WHERE status IN ('OPEN','ACKNOWLEDGED','ACCEPTED_RISK')
                DO UPDATE SET last_detected_at = now(), run_id = EXCLUDED.run_id""",
                        (run_id, f.finding_type, f.severity, f.fingerprint, f.securable, f.principal,
                         f.privilege, json.dumps(f.details, default=str)))
            if remediate_missing and f.finding_type == "MISSING_GRANT":
                key = GrantKey.of(f.details["securable_type"], f.securable, f.principal, f.privilege)
                if key in ent_ids:
                    cur.execute("UPDATE group_entitlement SET apply_status = 'PENDING' WHERE entitlement_id = %s",
                                (ent_ids[key],))
        # Anything open that was not seen this run has gone away - but only for checks that ran:
        # without Graph, membership findings were not re-evaluated and must stay open.
        not_checked = [] if graph is not None else ["MISSING_MEMBER", "UNEXPECTED_MEMBER"]
        cur.execute("UPDATE drift_finding SET status = 'RESOLVED', resolved_at = now(), resolved_by = %s "
                    "WHERE status IN ('OPEN','ACKNOWLEDGED') AND run_id <> %s AND finding_type <> ALL(%s)",
                    (ACTOR.email, run_id, not_checked))
        cur.execute("UPDATE drift_run SET status = 'SUCCEEDED', finished_at = now(), summary = %s WHERE run_id = %s",
                    (json.dumps(summary), run_id))
        audit.record(cur, ACTOR, "DRIFT_RUN", "DRIFT_RUN", run_id, after=summary)
        high = sum(1 for f in findings if f.severity == "HIGH")
        if high:
            notify.security_alert(cur, f"Access drift: {high} high-severity finding(s)",
                                  "Review open findings on the Audit page. " + json.dumps(summary),
                                  "DRIFT_HIGH", dedupe_key=f"drift:{run_id}")
    log.info("Drift run %s: %s", run_id, summary)
    return summary
