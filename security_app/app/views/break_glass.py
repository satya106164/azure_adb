import streamlit as st

from cdp_security import queries, service, ui
from cdp_security.config import get_settings
from cdp_security.models import AppRole

user = ui.require(AppRole.BREAK_GLASS)
settings = get_settings()
st.title("Emergency access")
ui.show_flash()
st.error(f"Use only for a live incident. Admins are alerted to add you at once; access lasts "
         f"{settings.break_glass_hours} hours from then. Security is alerted immediately and must review it within "
         f"{settings.break_glass_review_hours} hours.")

groups = queries.active_groups()
with st.form("bg"):
    group = st.selectbox("Access", groups, format_func=lambda g: f"{service.access_label(g)} - {g['group_name']} "
                                                                f"({g['classification_code'].lower()})")
    incident = st.text_input("Incident reference", placeholder="INC0012345")
    why = st.text_area("What is happening and why do you need this access?")
    confirm = st.checkbox("I understand this use is recorded and reviewed.")
    if st.form_submit_button("Get emergency access", type="primary", disabled=not groups):
        if not confirm:
            st.error("Confirm the statement to continue.")
        else:
            ui.act(lambda: service.break_glass(user, group["group_code"], incident, why),
                   "Emergency access recorded. Admins have been alerted to add you now.")
