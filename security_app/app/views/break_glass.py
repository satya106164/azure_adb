import streamlit as st

from cdp_security import kick, queries, service, ui
from cdp_security.config import get_settings
from cdp_security.models import AppRole

user = ui.require(AppRole.BREAK_GLASS)
settings = get_settings()
st.title("Emergency access")
ui.show_flash()
st.error(f"Use only for a live incident. Access starts at once, lasts {settings.break_glass_hours} hours, "
         "security is alerted immediately and must review it within "
         f"{settings.break_glass_review_hours} hours.")

groups = queries.active_groups()
with st.form("bg"):
    group = st.selectbox("Access", groups, format_func=lambda g: f"{g['group_name']} ({g['classification_code'].lower()})")
    incident = st.text_input("Incident reference", placeholder="INC0012345")
    why = st.text_area("What is happening and why do you need this access?")
    confirm = st.checkbox("I understand this use is recorded and reviewed.")
    if st.form_submit_button("Get emergency access", type="primary", disabled=not groups):
        if not confirm:
            st.error("Confirm the statement to continue.")
        else:
            ui.act(lambda: (service.break_glass(user, group["access_group_id"], incident, why),
                           kick.trigger_provisioning())[0],
                   "Emergency access granted. It is being applied now.")
