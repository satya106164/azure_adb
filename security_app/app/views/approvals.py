import streamlit as st

from cdp_security import queries, service, ui

user = ui.user()
ui.page_header("Approvals", "approvals")

tasks = service.tasks_for_user(user)
# Approval emails link to ?request=SAR-...: show that request first
wanted = st.query_params.get("request")
if wanted:
    if any(t["request_ref"] == wanted for t in tasks):
        st.info(f"You were sent here for **{wanted}**. It is shown first.")
        tasks = sorted(tasks, key=lambda t: t["request_ref"] != wanted)
    else:
        st.warning(f"**{wanted}** is not waiting for you: it has already been decided, or you are not one of "
                   "its approvers.")
if not tasks:
    st.info("Nothing is waiting for you.")
for t in tasks:
    head = f"{t['request_ref']} - {service.access_label(t)} for {t['beneficiary_email']}"
    if t["request_ref"] == wanted:
        head = f":blue-background[{head}]"
    with st.container(border=True):
        st.markdown(f"**{head}**")
        st.markdown(f"due {t['due_at']:%d %b %Y %H:%M} UTC" + (" :red[overdue]" if t["escalated_at"] else ""))
        c1, c2 = st.columns([2, 1])
        c1.write(f"Requested by {t['requester_email']} - group {t['group_name']}")
        c1.write(f"Justification: {t['justification']}")
        if t["requested_duration_days"]:
            c1.caption(f"Requested for {t['requested_duration_days']} days")
        if t["on_behalf_of"]:
            c1.caption(f"You are deciding as delegate for {t['on_behalf_of']}")
        comment = c2.text_area("Comment (required to reject)", key=f"c-{t['task_id']}")
        a, r = c2.columns(2)
        if a.button("Approve", key=f"a-{t['task_id']}", type="primary"):
            ui.act(lambda: service.decide_task(user, t["task_id"], True, comment), f"{t['request_ref']} approved.")
        if r.button("Reject", key=f"r-{t['task_id']}"):
            ui.act(lambda: service.decide_task(user, t["task_id"], False, comment), f"{t['request_ref']} rejected.")

with st.expander("My recent decisions"):
    ui.table(queries.my_decisions(user.email), ["request_ref", "data_product_name", "beneficiary_email", "task_status",
                                                "task_decided_at", "decision_comment"])
