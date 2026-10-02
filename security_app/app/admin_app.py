"""Catalog admin app (admins, auditors, security): reference data, groups, operations, audit."""
import logging

import streamlit as st

from cdp_security import ui
from cdp_security.config import get_settings
from cdp_security.models import AppRole

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
st.set_page_config(page_title="Catalog admin", page_icon=":material/account_tree:", layout="wide")

settings = get_settings()
user = ui.user()

# Databricks app permissions already limit who can open this app; check again here.
if not any(user.has(r) for r in (AppRole.ADMIN, AppRole.AUDITOR, AppRole.SECURITY)):
    st.error("This app is for platform admins, auditors and security only.")
    if settings.app_url:
        st.link_button("Go to the Data access app", settings.app_url)
    st.stop()

pages = []
if user.has(AppRole.ADMIN):
    pages += [st.Page("views/catalog_admin.py", title="Catalog", icon=":material/account_tree:"),
              st.Page("views/operations.py", title="Operations", icon=":material/sync_problem:")]
pages.append(st.Page("views/audit.py", title="Audit and drift", icon=":material/policy:", default=not pages))

ui.sidebar(user, "Catalog admin", ("Open Data access", settings.app_url))
st.navigation(pages).run()
