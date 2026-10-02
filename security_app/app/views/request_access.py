import streamlit as st

from cdp_security import queries, service, ui

user = ui.user()
st.title("Request access")
ui.show_flash()

groups = queries.requestable_groups()
if not groups:
    st.info("Nothing is open for requests yet. Ask a platform admin to register data products.")
    st.stop()

kinds = {"DATA_PRODUCT": "Data product", "AI_USE_CASE": "AI / Genie use case", "PROJECT": "Project",
         "WORKSPACE": "Workspace"}
present = [k for k in kinds if any(g["purpose"] == k for g in groups)]
kind = st.radio("What do you need?", present, format_func=kinds.get, horizontal=True)
options = [g for g in groups if g["purpose"] == kind]
group = st.selectbox(
    "Access", options,
    format_func=lambda g: f"{g['data_product_name'] or g['ai_use_case_name'] or g['project_name'] or g['workspace_name']}"
                          f" - {g['privilege_level'].lower()}")

cls = next(c for c in queries.classifications() if c["classification_code"] == group["classification_code"])
st.markdown(f"{ui.classification(cls['classification_code'])} - {cls['description']}")
if group.get("description"):
    st.caption(group["description"])
rules = queries.approval_rules(cls["classification_code"])
st.caption("Approval route: " + " then ".join(r["approver_role"].replace("_", " ").lower() for r in rules)
           + f". Maximum duration {cls['max_grant_days']} days.")

projects = queries.projects_for_group(group)
with st.form("request"):
    project = st.selectbox(
        "Project" + (" (required)" if cls["requires_project"] else " (optional)"),
        ([] if cls["requires_project"] else [None]) + projects,
        format_func=lambda p: "No project" if p is None else f"{p['code']} - {p['name']}")
    if cls["requires_project"] and not projects:
        st.warning("No active project is approved to use this data. Ask the data owner to link your project first.")
    for_someone_else = st.checkbox("I am requesting this for someone else")
    beneficiary = st.text_input("Their email", disabled=False) if for_someone_else else None
    days = st.number_input("For how many days?", 1, cls["max_grant_days"], min(90, cls["max_grant_days"]))
    why = st.text_area("Business justification", help="What will the access be used for? At least 20 characters.")
    ack = True
    if cls["classification_code"] != "GENERAL":
        ack = st.checkbox("I will handle this data under the data protection policy and only for the stated purpose.")
    if st.form_submit_button("Submit request", type="primary"):
        if not ack:
            st.error("Confirm the data handling statement to continue.")
        else:
            result = ui.act(lambda: service.submit_request(
                user, access_group_id=group["access_group_id"], justification=why, duration_days=int(days),
                project_id=project["project_id"] if project else None,
                beneficiary_email=beneficiary or None), rerun=False)
            if result:
                ui.flash(f"Request {result['request_ref']} submitted. You can follow it on My access.")
                st.switch_page("views/my_access.py")
