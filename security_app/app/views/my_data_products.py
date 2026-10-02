import streamlit as st

from cdp_security import queries, service, ui

user = ui.user()
st.title("My data products")
ui.show_flash()
st.caption("Data products and AI use cases you own or steward: which projects may use them, and who has access.")

owned = queries.owned_objects(user.email)
if not owned:
    st.info("You are not recorded as an owner or steward of any data product or AI use case.")
    st.stop()

ui.table(owned, ["object_kind", "code", "name", "role", "classification_code"])
dps = [o for o in owned if o["object_kind"] == "DATA_PRODUCT"]

if dps:
    st.subheader("Projects allowed to use your data")
    links = [l for l in queries.project_links() if l["data_product"] in {d["code"] for d in dps}]
    ui.table(links)
    owner_dps = [d for d in dps if d["role"] == "DATA_OWNER"]
    if owner_dps:
        with st.form("link"):
            st.markdown("Allow a project to request access to one of your data products")
            c1, c2 = st.columns(2)
            dp = c1.selectbox("Data product", owner_dps, format_func=lambda d: d["code"])
            projects = [p for p in queries.projects() if p["status"] == "ACTIVE"]
            proj = c2.selectbox("Project", projects, format_func=lambda p: f"{p['code']} - {p['name']}")
            why = st.text_input("Why does the project need it?")
            if st.form_submit_button("Allow project") and proj:
                ui.act(lambda: service.link_project_data_product(user, proj["project_id"], dp["object_id"], why),
                       f"{proj['code']} can now request {dp['code']}.")

st.subheader("Who has access")
grants = queries.grants_for_owned(user.email)
ui.table(grants, ["user_email", "group_name", "classification_code", "project_code", "status", "valid_to",
                  "last_recertified_at", "request_ref"])
active = [g for g in grants if g["status"] == "ACTIVE" and g["user_email"] != user.email]
if active:
    with st.form("revoke"):
        g = st.selectbox("Remove someone's access", active,
                         format_func=lambda r: f"{r['user_email']} - {r['group_name']}")
        note = st.text_input("Reason")
        if st.form_submit_button("Remove access"):
            ui.act(lambda: service.revoke_grant(user, g["grant_id"], note or None),
                   "Access will be removed within the next provisioning run.")
