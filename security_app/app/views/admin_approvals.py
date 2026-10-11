import streamlit as st

from cdp_security import queries, service, ui
from cdp_security.models import AppRole

user = ui.require(AppRole.ADMIN)
ui.page_header("Approvals", "admin_approvals")

tasks = queries.pending_approvals()
if not tasks:
    st.info("No requests are waiting for approval.")
    st.stop()
ui.table(tasks, ["request_ref", "beneficiary_email", "data_product_name", "group_name", "owner_email",
                 "approver2_email", "submitted_at", "due_at", "escalated_at"])

with st.form("override"):
    t = st.selectbox("Request", tasks,
                     format_func=lambda r: f"{r['request_ref']} - {r['beneficiary_email']} - {r['group_name']}"
                                           + (" (overdue)" if r["escalated_at"] else ""))
    reason = st.text_area("Reason for deciding on the approvers' behalf",
                          placeholder="Owner on long-term leave and approver 2 has left; confirmed with ...")
    a, r, _ = st.columns([1, 1, 4])
    approve = a.form_submit_button("Approve", type="primary")
    reject = r.form_submit_button("Reject")
    if approve or reject:
        ui.act(lambda: service.decide_task(user, t["task_id"], approve, reason, as_admin=True),
               f"{t['request_ref']} {'approved' if approve else 'rejected'} on behalf of the approvers.")
