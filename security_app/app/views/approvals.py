import streamlit as st

from cdp_security import kick, queries, service, ui

user = ui.user()
st.title("Approvals")
ui.show_flash()

tasks = service.tasks_for_user(user)
if not tasks:
    st.info("Nothing is waiting for you.")
for t in tasks:
    head = f"{t['request_ref']} - {t['group_name']} for {t['beneficiary_email']}"
    with st.container(border=True):
        st.markdown(f"**{head}**")
        st.markdown(f"{ui.classification(t['classification_code'])} "
                    + ("  :red[**Break-glass review**]" if t["is_post_hoc"] else
                       f"stage {t['stage_no']} of {t['total_stages']} ({t['approver_role'].replace('_', ' ').lower()})")
                    + f" - due {t['due_at']:%d %b %Y %H:%M} UTC" + (" :red[overdue]" if t["escalated_at"] else ""))
        c1, c2 = st.columns([2, 1])
        c1.write(f"Requested by {t['requester_email']}"
                 + (f" for project {t['project_code']}" if t["project_code"] else "")
                 + (f", incident {t['incident_ref']}" if t["incident_ref"] else ""))
        c1.write(f"Justification: {t['justification']}")
        if t["requested_duration_days"]:
            c1.caption(f"Requested for {t['requested_duration_days']} days")
        if t["on_behalf_of"]:
            c1.caption(f"You are deciding as delegate for {t['on_behalf_of']}")
        comment = c2.text_area("Comment (required to reject)", key=f"c-{t['task_id']}")
        a, r = c2.columns(2)
        if a.button("Approve", key=f"a-{t['task_id']}", type="primary"):
            ui.act(lambda: (service.decide_task(user, t["task_id"], True, comment), kick.trigger_provisioning())[0],
                   f"{t['request_ref']} approved.")
        if r.button("Reject", key=f"r-{t['task_id']}"):
            ui.act(lambda: service.decide_task(user, t["task_id"], False, comment), f"{t['request_ref']} rejected.")

with st.expander("My recent decisions"):
    ui.table(queries.my_decisions(user.email), ["request_ref", "group_name", "beneficiary_email", "task_status",
                                                "task_decided_at", "decision_comment"])
