import streamlit as st

from cdp_security import queries, service, templates, ucsql, ui
from cdp_security.models import AppRole

user = ui.require(AppRole.ADMIN)
st.title("Catalog admin")
ui.show_flash()
st.caption("Register what is governed and who owns it. Approval routes and classification limits change "
           "only through a reviewed database migration.")

CLASSES = ["GENERAL", "CONFIDENTIAL", "RESTRICTED"]
ws_rows = queries.workspaces()
dp_rows = queries.data_products()


def ws_label(w):
    return "None" if w is None else f"{w['name']} ({w['workspace_type'].lower()}, {w['environment']})"


tabs = st.tabs(["Workspaces", "Data products", "Projects", "AI and BI assets", "Groups and entitlements",
                "Owners and roles", "Rules"])

with tabs[0]:
    ui.table(ws_rows, ["name", "workspace_type", "environment", "host_url", "is_active"])
    with st.form("ws"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", placeholder="dbanalytics-prd")
        wtype = c2.selectbox("Type", ["INGESTION", "PROJECT", "DBANALYTICS", "ANALYTICS_PBI", "MLOPS"])
        host = c1.text_input("Workspace URL", placeholder="https://adb-1234567890123456.7.azuredatabricks.net")
        env = c2.selectbox("Environment", ["dev", "tst", "prd"])
        wid = c1.number_input("Workspace id (optional)", min_value=0, step=1, value=0)
        if st.form_submit_button("Register workspace"):
            ui.act(lambda: service.register_workspace(user, name=name, workspace_type=wtype, host_url=host,
                                                      environment=env, databricks_workspace_id=wid or None),
                   f"Workspace {name} registered.")

with tabs[1]:
    ui.table(dp_rows, ["code", "name", "catalog_name", "schema_name", "classification_code", "lifecycle_status"])
    with st.form("dp"):
        c1, c2 = st.columns(2)
        code = c1.text_input("Code", placeholder="member_contributions")
        name = c2.text_input("Name", placeholder="Member contributions")
        desc = st.text_area("Description")
        catalog = c1.text_input("Catalog", value="dbanalytics")
        schema = c2.text_input("Schema (defaults to the code)")
        cls = c1.selectbox("Classification", CLASSES)
        home = c2.selectbox("Home workspace", [None] + ws_rows, format_func=ws_label)
        owner = c1.text_input("Data owner email")
        steward = c2.text_input("Data steward email")
        lgs = st.multiselect("Logical groups", [g["logical_group_code"] for g in queries.logical_groups()])
        create = st.checkbox("Create read and write groups with default grants", value=True)
        if st.form_submit_button("Register data product"):
            ui.act(lambda: service.register_data_product(
                user, code=code, name=name, description=desc, catalog=catalog, schema=schema or code,
                classification_code=cls, owner_email=owner, steward_email=steward,
                home_workspace_id=home["workspace_id"] if home else None, logical_groups=lgs, create_groups=create),
                f"Data product {code} registered.")

with tabs[2]:
    projects = queries.projects()
    ui.table(projects, ["code", "name", "sponsor_email", "status", "start_date", "end_date"])
    with st.form("project"):
        c1, c2 = st.columns(2)
        code = c1.text_input("Code", placeholder="contribution_forecast_2026")
        name = c2.text_input("Name")
        desc = st.text_area("Description", key="pdesc")
        sponsor = c1.text_input("Sponsor email")
        ws = c2.selectbox("Project workspace", [None] + ws_rows, format_func=ws_label)
        end = c1.date_input("End date", value=None)
        create = c2.checkbox("Create a contributors group", value=True)
        if st.form_submit_button("Register project"):
            ui.act(lambda: service.register_project(user, code=code, name=name, description=desc,
                                                    sponsor_email=sponsor,
                                                    workspace_id=ws["workspace_id"] if ws else None,
                                                    end_date=end, create_group=create),
                   f"Project {code} registered.")
    st.markdown("**Data products a project may use**")
    ui.table(queries.project_links())
    with st.form("link"):
        c1, c2 = st.columns(2)
        p = c1.selectbox("Project", projects, format_func=lambda r: r["code"])
        d = c2.selectbox("Data product", dp_rows, format_func=lambda r: r["code"])
        why = st.text_input("Why does the project need it?")
        if st.form_submit_button("Link"):
            ui.act(lambda: service.link_project_data_product(user, p["project_id"], d["data_product_id"], why),
                   "Linked.")

with tabs[3]:
    asset_rows = queries.assets()
    ui.table(asset_rows, ["asset_type", "name", "external_id", "workspace_name", "classification_code", "is_active"])
    with st.form("asset"):
        c1, c2 = st.columns(2)
        atype = c1.selectbox("Type", list(templates.ASSET_PRIVILEGES))
        name = c2.text_input("Name", key="aname")
        ext = c1.text_input("External id", help="Genie space id, endpoint name, dashboard id, Power BI workspace GUID, "
                                                "or '<workspace GUID>/<dataset GUID>' for a semantic model")
        ws = c2.selectbox("Databricks workspace (not for Power BI)", [None] + ws_rows, format_func=ws_label)
        dp = c1.selectbox("Data product", [None] + dp_rows, format_func=lambda r: "None" if r is None else r["code"])
        cls = c2.selectbox("Classification", CLASSES, key="acls")
        if st.form_submit_button("Register asset"):
            ui.act(lambda: service.register_asset(user, asset_type=atype, name=name, external_id=ext,
                                                  workspace_id=ws["workspace_id"] if ws else None,
                                                  data_product_id=dp["data_product_id"] if dp else None,
                                                  classification_code=cls), f"Asset {name} registered.")
    st.markdown("**AI and Genie use cases** - each gets a -user and an -admin group")
    ui.table(queries.ai_use_cases(), ["code", "name", "data_product_code", "classification_code", "status"])
    with st.form("ai"):
        c1, c2 = st.columns(2)
        code = c1.text_input("Code", key="aicode", placeholder="member_contributions_genie")
        name = c2.text_input("Name", key="ainame")
        desc = st.text_area("Description", key="aidesc")
        dp = c1.selectbox("Data product", dp_rows, format_func=lambda r: r["code"], key="aidp")
        owner = c2.text_input("Use case owner email")
        picks = st.multiselect("Assets", asset_rows, format_func=lambda a: f"{a['asset_type']}: {a['name']}")
        read = st.checkbox("Also grant read on the data product (needed for Genie answers)", value=True)
        if st.form_submit_button("Register AI use case"):
            ui.act(lambda: service.register_ai_use_case(
                user, code=code, name=name, description=desc, data_product_id=dp["data_product_id"],
                asset_ids=[a["asset_id"] for a in picks], owner_email=owner, include_data_product_read=read),
                f"AI use case {code} registered.")

with tabs[4]:
    groups = queries.groups()
    ui.table(groups, ["group_name", "status", "purpose", "privilege_level", "classification_code", "requestable",
                      "entra_object_id", "owner_email"])
    if groups:
        g = st.selectbox("Group", groups, format_func=lambda r: f"{r['group_name']} ({r['status'].lower()})")
        ents = queries.entitlements(g["access_group_id"])
        ui.table(ents, ["target_type", "target", "privilege", "desired_state", "apply_status", "last_applied_at",
                        "last_error"])
        c1, c2 = st.columns(2)
        with c1.form("ent-uc"):
            st.markdown("Add a Unity Catalog grant")
            stype = st.selectbox("Securable type", list(ucsql.ALLOWED_PRIVILEGES))
            fname = st.text_input("Full name", placeholder="dbanalytics.member_contributions")
            priv = st.selectbox("Privilege", sorted(set().union(*ucsql.ALLOWED_PRIVILEGES.values())))
            if st.form_submit_button("Add grant"):
                ui.act(lambda: service.add_entitlement(user, g["access_group_id"], priv, securable_type=stype,
                                                       securable_full_name=fname), "Entitlement added.")
        with c2.form("ent-asset"):
            st.markdown("Add an asset permission")
            a = st.selectbox("Asset", queries.assets(), format_func=lambda r: f"{r['asset_type']}: {r['name']}")
            apriv = st.selectbox("Permission", sorted(set().union(*templates.ASSET_PRIVILEGES.values())))
            if st.form_submit_button("Add permission"):
                ui.act(lambda: service.add_entitlement(user, g["access_group_id"], apriv, asset_id=a["asset_id"]),
                       "Entitlement added.")
        members = queries.group_members(g["access_group_id"])
        st.markdown(f"**Members ({len(members)})**")
        ui.table(members, ["user_email", "status", "valid_to", "last_recertified_at", "is_break_glass", "request_ref"])
        active_members = [m for m in members if m["status"] == "ACTIVE"]
        if active_members:
            with st.form("member-revoke"):
                m = st.selectbox("Remove a member", active_members, format_func=lambda r: r["user_email"])
                why = st.text_input("Reason", key="member-why")
                if st.form_submit_button("Remove member"):
                    ui.act(lambda: service.revoke_grant(user, m["grant_id"], why or None),
                           "Member will be removed within the next provisioning run.")
        live = [e for e in ents if e["desired_state"] == "PRESENT"]
        if live:
            e = st.selectbox("Remove an entitlement", [None] + live,
                             format_func=lambda r: "Choose" if r is None else f"{r['privilege']} on {r['target']}")
            if e and st.button("Remove entitlement"):
                ui.act(lambda: service.remove_entitlement(user, e["entitlement_id"]), "Entitlement will be removed.")
        if g["status"] == "PLANNED":
            with st.form("objid"):
                st.markdown("Group created by the identity team? Record its Entra object id to activate it.")
                oid = st.text_input("Entra object id")
                if st.form_submit_button("Activate group"):
                    ui.act(lambda: service.set_group_object_id(user, g["access_group_id"], oid), "Group activated.")
        if g["status"] == "ACTIVE":
            with st.form("retire"):
                reason = st.text_input("Reason for retiring this group")
                if st.form_submit_button("Retire group (removes all members and grants)"):
                    ui.act(lambda: service.retire_group(user, g["access_group_id"], reason), "Group is being retired.")

with tabs[5]:
    owns = queries.ownerships()
    ui.table(owns, ["object_kind", "object", "role", "email", "valid_from"])
    with st.form("own"):
        c1, c2 = st.columns(2)
        kind = c1.selectbox("Object type", ["DATA_PRODUCT", "PROJECT", "AI_USE_CASE", "ACCESS_GROUP", "ASSET"])
        choices = {
            "DATA_PRODUCT": [(r["data_product_id"], r["code"]) for r in dp_rows],
            "PROJECT": [(r["project_id"], r["code"]) for r in queries.projects()],
            "AI_USE_CASE": [(r["ai_use_case_id"], r["code"]) for r in queries.ai_use_cases()],
            "ACCESS_GROUP": [(r["access_group_id"], r["group_name"]) for r in queries.groups()],
            "ASSET": [(r["asset_id"], r["name"]) for r in queries.assets()],
        }[kind]
        obj = c2.selectbox("Object", choices, format_func=lambda x: x[1])
        role = c1.selectbox("Role", ["DATA_OWNER", "STEWARD", "TECHNICAL_OWNER"])
        email = c2.text_input("Person's email")
        if st.form_submit_button("Assign") and obj:
            ui.act(lambda: service.assign_ownership(user, kind, obj[0], email, role), "Assigned.")
    if owns:
        o = st.selectbox("End an assignment", [None] + owns,
                         format_func=lambda r: "Choose" if r is None else f"{r['object']} {r['role']} {r['email']}")
        if o and st.button("End assignment"):
            ui.act(lambda: service.end_ownership(user, o["ownership_id"]), "Assignment ended.")
    st.markdown("**App roles (fallback to Entra groups)**")
    ui.table(queries.app_role_members())
    with st.form("roles"):
        c1, c2, c3 = st.columns(3)
        email = c1.text_input("Email", key="roleemail")
        role = c2.selectbox("Role", [r.value for r in AppRole])
        grant = c3.radio("Action", ["Grant", "Remove"], horizontal=True)
        if st.form_submit_button("Save role"):
            ui.act(lambda: service.set_app_role(user, email, AppRole(role), grant == "Grant"), "Role saved.")

with tabs[6]:
    st.markdown("**Classifications**")
    ui.table(queries.classifications())
    st.markdown("**Approval route per classification**")
    ui.table(queries.approval_rules())
