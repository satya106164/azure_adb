import csv
import io
from datetime import datetime, time, timedelta, timezone

import streamlit as st

from cdp_security import queries, service, ui
from cdp_security.models import AppRole

user = ui.require(AppRole.ADMIN, AppRole.AUDITOR, AppRole.SECURITY)
st.title("Audit")
ui.show_flash()


def to_csv(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode()


tab_audit, tab_evidence = st.tabs(["Audit trail", "Evidence pack"])

with tab_audit:
    c1, c2, c3, c4 = st.columns(4)
    since = c1.date_input("From", datetime.now().date() - timedelta(days=7))
    until = c2.date_input("To", datetime.now().date())
    actor = c3.text_input("Actor contains")
    text = c4.text_input("Reference / object / action")
    rows = queries.audit_events(datetime.combine(since, time.min, tzinfo=timezone.utc),
                                datetime.combine(until, time.max, tzinfo=timezone.utc), actor or None, text or None)
    ui.table(rows, ["chain_seq", "occurred_at", "actor", "action", "object_kind", "object_id", "correlation_id",
                    "reason"])
    st.download_button("Download CSV", to_csv(rows), "audit_events.csv", "text/csv", disabled=not rows)
    if st.button("Verify audit chain integrity"):
        broken = service.verify_audit_chain()
        if broken:
            st.error(f"Audit chain is broken at {len(broken)} point(s). Escalate to security immediately.")
            ui.table(broken)
        else:
            st.success("Audit chain verified: no gaps and every hash matches.")

with tab_evidence:
    st.caption("Every live grant with its approval trail and who added the member. Use for recertification "
               "evidence and regulator requests.")
    evidence = queries.access_evidence()
    ui.table(evidence)
    st.download_button("Download evidence CSV", to_csv(evidence),
                       f"access_evidence_{datetime.now():%Y%m%d}.csv", "text/csv", disabled=not evidence)
