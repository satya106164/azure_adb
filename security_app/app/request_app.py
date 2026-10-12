"""Data access app (everyone): request, approve, review, approver tools."""
import logging

import streamlit as st

from cdp_security import ui
from cdp_security.config import get_settings
from cdp_security.models import AppRole

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
st.set_page_config(page_title="Data access", page_icon=":material/shield_person:", layout="wide")

settings = get_settings()
user = ui.user()

pages = {
    "Access": [
        st.Page("views/my_access.py", title="My access", icon=":material/badge:", default=True),
        st.Page("views/request_access.py", title="Request access", icon=":material/add_moderator:"),
    ],
    "Reviews": [
        st.Page("views/approvals.py", title="Approvals", icon=":material/fact_check:"),
        st.Page("views/recertification.py", title="Access reviews", icon=":material/event_repeat:"),
        st.Page("views/my_data_products.py", title="My data products", icon=":material/database:"),
    ],
}

admin_link = None
if user.has(AppRole.ADMIN):
    admin_link = ("Open Catalog admin", settings.admin_app_url)
ui.sidebar(user, "Data access", admin_link)
st.navigation(pages).run()
