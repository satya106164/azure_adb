from datetime import date, timedelta

import streamlit as st

from cdp_security import directory, queries, service, ui

user = ui.user()
ui.page_header("Request access", "request_access")

groups = queries.requestable_groups()
if not groups:
    st.info("Nothing is open for requests yet. Ask a platform admin to register data products and their groups.")
    st.stop()

group = st.selectbox(
    "Data product", groups,
    format_func=lambda g: f"{service.access_label(g)} - {g['data_product_code']}"
                          + (f" - {g['group_name']}" if sum(x["data_product_code"] == g["data_product_code"]
                                                            for x in groups) > 1 else ""))
where = f" Data in `{group['catalog_name']}.{group['schema_name']}`." if group["schema_name"] else ""
st.caption(f"Access through group {group['group_name']}.{where} " + (group["description"] or ""))
st.caption(f"Approved by {group['owner_email']} or {group['approver2_email']}.")

OTHER = {"email": "", "display_name": "Someone not in the list (type their email)"}


@st.cache_data(ttl=600, show_spinner="Loading people...")
def people() -> list[dict]:
    return directory.people()


# Outside the form, so choosing "someone else" shows the person picker straight away
beneficiary = None
if st.checkbox("I am requesting this for someone else"):
    others = [p for p in people() if p["email"] != user.email]
    who = st.selectbox("Who is it for?", others + [OTHER], index=None, placeholder="Start typing a name or email",
                       format_func=lambda p: p["display_name"] if p["email"] == ""
                       else f"{p['display_name']} ({p['email']})" if p["display_name"] != p["email"] else p["email"])
    if who is None:
        beneficiary = ""  # ticked but nobody chosen: refused on submit, never silently "myself"
    elif who["email"] == "":
        beneficiary = st.text_input("Their work email").strip()
    else:
        beneficiary = who["email"]

with st.form("request"):
    today = date.today()
    until = st.date_input("Access until", value=date(2030, 1, 1), min_value=today + timedelta(days=1),
                          max_value=date(2099, 12, 31), format="DD-MM-YYYY",
                          help="Your approvers see this date. Access is also re-confirmed in periodic reviews.")
    why = st.text_area("Business justification", help="What will the access be used for? At least 20 characters.")
    if st.form_submit_button("Submit request", type="primary"):
        if beneficiary == "":
            st.error("Choose who the access is for, or untick \"for someone else\".")
        else:
            result = ui.act(lambda: service.submit_request(
                user, group_code=group["group_code"], justification=why, duration_days=(until - today).days,
                beneficiary_email=beneficiary or None), rerun=False)
            if result:
                ui.flash(f"Request {result['request_ref']} submitted. You can follow it on My access.")
                st.switch_page("views/my_access.py")
