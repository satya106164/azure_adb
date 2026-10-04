import streamlit as st

from cdp_security import queries, service, ui
from cdp_security.models import AppRole

user = ui.require(AppRole.ADMIN)
st.title("Fulfilment")
ui.show_flash()
st.caption("The app never changes a group. Add or remove the person in Entra ID (or Databricks) yourself, then "
           "record it here: who did it and when goes into the audit trail and the reports. You cannot record "
           "access given to yourself.")


def access(row: dict) -> str:
    return f"{row['user_email']} - {row['group_name']} ({service.access_label(row)})"


tab_add, tab_remove, tab_done = st.tabs(["To add", "To remove", "Done (30 days)"])

with tab_add:
    rows = queries.to_grant()
    ui.table(rows, ["request_ref", "user_email", "group_name", "data_product_name", "classification_code",
                    "is_break_glass", "incident_ref", "decided_at"])
    if rows:
        with st.form("grant"):
            g = st.selectbox("Member added to the group", rows, format_func=access)
            note = st.text_input("Note (optional)", placeholder="Added in Entra ID, change ticket CHG0012345")
            if st.form_submit_button("Mark as granted", type="primary"):
                ui.act(lambda: service.mark_granted(user, g["grant_id"], note),
                       f"{g['user_email']} recorded as added to {g['group_name']}.")

with tab_remove:
    rows = queries.to_remove()
    ui.table(rows, ["request_ref", "user_email", "group_name", "data_product_name", "why", "revoke_requested_at",
                    "valid_to", "is_break_glass"])
    if rows:
        with st.form("remove"):
            g = st.selectbox("Member removed from the group", rows, format_func=access)
            note = st.text_input("Note (optional)", key="remove-note")
            if st.form_submit_button("Mark as removed", type="primary"):
                ui.act(lambda: service.mark_removed(user, g["grant_id"], note),
                       f"{g['user_email']} recorded as removed from {g['group_name']}.")

with tab_done:
    ui.table(queries.recent_manual_changes(), ["changed_at", "change", "user_email", "group_name",
                                               "data_product_name", "changed_by", "note", "request_ref"])

st.subheader("Notifications that could not be sent")
ui.table(queries.failed_notifications())
