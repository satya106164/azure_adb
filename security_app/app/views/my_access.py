import streamlit as st

from cdp_security import queries, service, ui

user = ui.user()
st.title("My access")
ui.show_flash()

grants = queries.my_grants(user.email)
st.subheader("What you can access")
if not grants:
    st.info("You have no access through this app yet. Use Request access to ask for some.")
for g in grants:
    ends = f"until {g['valid_to']:%d %b %Y}" if g["status"] == "ACTIVE" else ui.STATUS_LABELS.get(g["status"], "")
    with st.expander(f"{service.access_label(g)} - {ends}"):
        st.markdown(f"{ui.classification(g['classification_code'])} {ui.status(g['status'])}"
                    + ("  :red[break-glass]" if g["is_break_glass"] else ""))
        st.caption(f"Group {g['group_name']} - request {g['request_ref']}")
        if g["status"] != "ACTIVE":
            continue
        left, right = st.columns(2)
        with left.form(f"extend-{g['grant_id']}"):
            days = st.number_input("Extend by (days)", 1, 3650, 90)
            why = st.text_area("Why do you still need it?", key=f"why-{g['grant_id']}")
            if st.form_submit_button("Request extension", disabled=g["is_break_glass"]):
                ui.act(lambda: service.request_extension(user, g["grant_id"], int(days), why),
                       "Extension requested. Your approvers have been notified.")
        with right:
            st.write("No longer need it?")
            if st.button("Give up this access", key=f"revoke-{g['grant_id']}"):
                ui.act(lambda: service.revoke_grant(user, g["grant_id"], "Given up by the user"),
                       "An admin will remove you from the group; you will be told when it is done.")

st.subheader("My requests")
requests_ = queries.my_requests(user.email)
ui.table(requests_, ["request_ref", "request_type", "status", "data_product_name", "group_name",
                     "classification_code", "beneficiary_email", "current_stage_no", "total_stages", "submitted_at"])
cancellable = [r for r in requests_ if r["status"] == "PENDING_APPROVAL"]
if cancellable:
    pick = st.selectbox("Cancel a request", [None] + cancellable,
                        format_func=lambda r: "Choose a request" if r is None else f"{r['request_ref']} - {r['group_name']}")
    if pick:
        ui.table(queries.request_tasks(pick["request_id"]))
        if st.button("Cancel request"):
            ui.act(lambda: service.cancel_request(user, pick["request_id"]), f"{pick['request_ref']} cancelled.")

st.subheader("Approval cover while you are away")
ui.table(queries.delegations_for(user.email), ["delegator", "delegate", "valid_from", "valid_to", "reason"])
with st.form("delegate"):
    c1, c2 = st.columns(2)
    delegate = c1.text_input("Delegate's email")
    until = c2.date_input("Until (max 60 days)")
    reason = st.text_input("Reason", placeholder="Annual leave")
    if st.form_submit_button("Add delegate"):
        from datetime import datetime, time, timezone

        ui.act(lambda: service.add_delegation(user, user.email, delegate,
                                              datetime.combine(until, time(23, 59), tzinfo=timezone.utc), reason),
               "Delegation added.")
