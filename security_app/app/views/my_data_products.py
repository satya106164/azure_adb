import streamlit as st

from cdp_security import queries, service, ui

user = ui.user()
ui.page_header("My data products", "my_data_products")

owned = queries.owned_data_products(user.email)
if not owned:
    st.info("You are not recorded as an approver of any data product.")
    st.stop()
ui.table([{**r, "data_product_type": service.DATA_PRODUCT_TYPES[r["data_product_type"]]} for r in owned],
         ["data_product_code", "name", "data_product_type", "environment", "project_name", "catalog_name",
          "schema_name", "role"])

st.subheader("Who has access")
grants = queries.grants_for_owned(user.email)
ui.table(grants, ["user_email", "data_product_name", "group_name", "status", "valid_to",
                  "last_recertified_at", "request_ref"])
# Either approver may remove access (FR-5a)
active = [g for g in grants if g["status"] == "ACTIVE" and user.email in (g["owner_email"], g["approver2_email"])
          and g["user_email"] != user.email]
if active:
    with st.form("revoke"):
        g = st.selectbox("Remove someone's access", active,
                         format_func=lambda r: f"{r['user_email']} - {r['data_product_name']}")
        note = st.text_input("Reason")
        if st.form_submit_button("Remove access"):
            ui.act(lambda: service.revoke_grant(user, g["grant_id"], note or None),
                   "An admin will remove them from the group.")
