import streamlit as st

from cdp_security import kick, queries, service, ui
from cdp_security.models import AppRole

user = ui.require(AppRole.ADMIN)
st.title("Operations")
ui.show_flash()

st.subheader("Provisioning queue (last 30 days)")
ui.table(queries.queue_summary())

st.subheader("Needs attention")
dead = queries.actions(["DEAD"])
ui.table(dead, ["action_type", "group_name", "user_email", "attempts", "last_error", "created_at"])
if dead:
    a = st.selectbox("Retry an action after fixing its cause", dead,
                     format_func=lambda r: f"{r['action_type']} {r['group_name'] or ''} {r['user_email'] or ''}")
    if st.button("Retry action", type="primary"):
        ui.act(lambda: (service.retry_action(user, a["action_id"]), kick.trigger_provisioning())[0],
               "Action re-queued.")

st.subheader("In flight")
ui.table(queries.actions(["QUEUED", "IN_PROGRESS", "FAILED"]),
         ["action_type", "status", "group_name", "user_email", "attempts", "next_attempt_at", "last_error"])

st.subheader("Notifications that could not be sent")
ui.table(queries.failed_notifications())
