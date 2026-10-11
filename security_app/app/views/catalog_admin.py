import streamlit as st

from cdp_security import naming, queries, service, ui, unity
from cdp_security.config import get_settings
from cdp_security.models import AppRole

user = ui.require(AppRole.ADMIN)
settings = get_settings()
st.title("Catalog admin")
ui.show_flash()
st.markdown("Register workspaces, data products and their access groups. Identifiers (WS-, DP-, GRP-) are "
            "generated for you.")

WS_TYPES = list(service.WORKSPACE_TYPES)
DP_TYPES = list(service.DATA_PRODUCT_TYPES)
ENVS = list(service.ENVIRONMENTS)
ws_rows = queries.workspaces()
dp_rows = queries.data_products()
ws_codes = [None] + [w["workspace_code"] for w in ws_rows]
ws_names = {w["workspace_code"]: w["name"] for w in ws_rows}


def ws_label(code):
    return "None" if code is None else f"{code} {ws_names.get(code, '')}"


def dp_label(row: dict) -> str:
    return f"{row['data_product_code']} {row['name']} ({service.DATA_PRODUCT_TYPES[row['data_product_type']]})"


NONE, TYPED = "(none)", "Type a name..."


@st.cache_data(ttl=600, show_spinner="Loading catalogs...")
def all_catalogs() -> list[str]:
    return unity.catalogs()


@st.cache_data(ttl=600, show_spinner="Loading schemas...")
def schemas_in(catalog: str) -> list[str]:
    return unity.schemas(catalog)


def choose(col, label: str, names: list[str], current: str | None, key: str, help: str) -> str | None:
    """Pick from names (the current value always offered), none, or type a name not in the list."""
    options = [NONE] + sorted(set(names) | ({current} if current else set())) + [TYPED]
    picked = col.selectbox(label, options, index=options.index(current) if current else 0, key=key, help=help)
    if picked == TYPED:
        return col.text_input(f"{label} name", key=f"{key}-typed").strip() or None
    return None if picked == NONE else picked


def pick(label: str, rows: list[dict], key: str, fmt):
    """Select an existing row to edit, or None for 'register new'."""
    return st.selectbox(label, [None] + rows, key=key,
                        format_func=lambda r: "Register a new one" if r is None else fmt(r))


tab_ws, tab_dp, tab_grp = st.tabs(["Workspaces", "Data products", "Groups"])

# ---- Workspaces ----------------------------------------------------------------------
with tab_ws:
    ui.instructions("catalog_workspaces")
    ui.table([{**r, "workspace_type": service.WORKSPACE_TYPES.get(r["workspace_type"], r["workspace_type"])}
              for r in ws_rows],
             ["workspace_code", "name", "workspace_type", "environment", "host_url", "databricks_workspace_id",
              "is_active"])
    w = pick("Edit or register", ws_rows, "ws-pick", lambda r: f"{r['workspace_code']} {r['name']}")
    with st.form(f"ws-{w['workspace_code'] if w else 'new'}"):
        st.markdown(f"**{'Edit ' + w['workspace_code'] if w else 'Register a workspace'}**")
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", value=w["name"] if w else "", placeholder="analytics-prd")
        wtype = c2.selectbox("Type", WS_TYPES, format_func=service.WORKSPACE_TYPES.get,
                             index=WS_TYPES.index(w["workspace_type"]) if w and w["workspace_type"] in WS_TYPES
                             else 0)
        host = c1.text_input("Workspace URL", value=w["host_url"] if w else "",
                             placeholder="https://adb-1234567890123456.7.azuredatabricks.net",
                             help="The workspace id is read from the URL.")
        env = c2.selectbox("Environment", ENVS, format_func=service.ENVIRONMENTS.get,
                           index=ENVS.index(w["environment"]) if w and w["environment"] in ENVS else 0)
        active = c2.checkbox("Active", value=w["is_active"] if w else True, disabled=w is None)
        if st.form_submit_button("Save changes" if w else "Register workspace", type="primary"):
            if w:
                ui.act(lambda: service.update_workspace(user, w["workspace_code"], name=name, workspace_type=wtype,
                                                        host_url=host, environment=env, is_active=active),
                       f"Workspace {w['workspace_code']} saved.")
            else:
                ui.act(lambda: ui.flash_result(service.register_workspace(
                    user, name=name, workspace_type=wtype, host_url=host, environment=env),
                    "workspace_code", "Workspace {} registered."))

# ---- Data products -----------------------------------------------------------------
with tab_dp:
    ui.instructions("catalog_data_products")
    ui.table([{**r, "data_product_type": service.DATA_PRODUCT_TYPES[r["data_product_type"]]} for r in dp_rows],
             ["data_product_code", "name", "data_product_type", "environment", "project_name", "catalog_name",
              "schema_name", "owner_email", "approver2_email", "workspace_name", "lifecycle_status"])
    d = pick("Edit or register", dp_rows, "dp-pick", dp_label)
    dp_key = d["data_product_code"] if d else "new"
    st.markdown(f"**{'Edit ' + d['data_product_code'] if d else 'Register a data product'}**")
    # Outside the form, so the catalog and schema lists follow each choice straight away
    c1, c2 = st.columns(2)
    dp_env = c1.selectbox("Environment", ENVS, format_func=service.ENVIRONMENTS.get, key=f"dp-env-{dp_key}",
                          index=ENVS.index(d["environment"]) if d and d.get("environment") in ENVS else 0)
    show_all = c2.checkbox("Show all catalogs", key=f"dp-allcat-{dp_key}",
                           help="Unticked, only catalogs named for the environment are listed (dev_..., prd_...).")
    found = all_catalogs()
    if not found:
        st.caption("Catalogs could not be listed (the app needs BROWSE on them); type the name instead.")
    catalog = choose(c1, "Catalog", found if show_all else unity.for_environment(found, dp_env),
                     d["catalog_name"] if d else None, f"dp-cat-{dp_key}-{dp_env}-{show_all}",
                     "Required for the Schema type; optional for the others.")
    saved_schema = d["schema_name"] if d and d["catalog_name"] == catalog else None
    schema = choose(c2, "Schema", schemas_in(catalog) if catalog else [], saved_schema,
                    f"dp-sch-{dp_key}-{catalog}", "Required for the Schema type; optional for the others.")
    with st.form(f"dp-{dp_key}"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", value=d["name"] if d else "", placeholder="Member contributions")
        dtype = c2.selectbox("Type", DP_TYPES, format_func=service.DATA_PRODUCT_TYPES.get,
                             index=DP_TYPES.index(d["data_product_type"]) if d else 0)
        desc = st.text_area("Description", value=(d["description"] or "") if d else "")
        project = st.text_input("Project (optional)", value=(d["project_name"] or "") if d else "")
        owner = c1.text_input("Owner / approver email", value=d["owner_email"] if d else "",
                              help="Approves requests for every group of this data product.")
        approver2 = c2.text_input("Approver 2 email", value=d["approver2_email"] if d else "",
                                  help="Can also approve, e.g. when the owner is away. Must be someone else.")
        home = c1.selectbox("Home workspace", ws_codes, format_func=ws_label,
                            index=ws_codes.index(d["home_workspace_code"]) if d and d["home_workspace_code"]
                            in ws_codes else 0)
        status = c2.selectbox("Status", ["ACTIVE", "RETIRED"], disabled=d is None,
                              index=["ACTIVE", "RETIRED"].index(d["lifecycle_status"]) if d else 0,
                              help="Retired data products take no new requests or groups. Retire their groups "
                                   "to remove existing access.")
        if st.form_submit_button("Save changes" if d else "Register data product", type="primary"):
            values = dict(name=name, description=desc, data_product_type=dtype, environment=dp_env,
                          project=project, catalog=catalog, schema=schema, owner_email=owner,
                          approver2_email=approver2, home_workspace_code=home)
            if d:
                ui.act(lambda: service.update_data_product(user, d["data_product_code"], lifecycle_status=status,
                                                           **values), f"Data product {d['data_product_code']} saved.")
            else:
                ui.act(lambda: ui.flash_result(service.register_data_product(user, **values), "data_product_code",
                                               "Data product {} registered. Add its access group on the Groups tab."))


# ---- Groups ----------------------------------------------------------------------------
def group_form(g: dict | None, dps: list[dict]) -> None:
    """Register (g is None) or edit a group. The checks must pass with these exact values before saving."""
    key = g["group_code"] if g else "new"
    c1, c2 = st.columns(2)
    current = next((i for i, r in enumerate(dps) if g and r["data_product_code"] == g["data_product_code"]), 0)
    dp = c1.selectbox("Data product", dps, index=current, key=f"grp-dp-{key}", format_func=dp_label)
    default_name = g["group_name"] if g else naming.suggest_group_name(settings.env, dp["name"])
    gname = c2.text_input("Group name (as it is in Entra ID)", value=default_name, key=f"grp-name-{key}-"
                          + ("" if g else dp["data_product_code"]))
    desc = c1.text_input("Description (optional)", value=(g["description"] or "") if g else "", key=f"grp-desc-{key}")
    requestable = c2.checkbox("Open for self-service requests", value=g["requestable"] if g else True,
                              key=f"grp-req-{key}")
    values = (gname.strip(), dp["data_product_code"])
    state_key = f"grp-checked-{key}"
    a, b, _ = st.columns([1, 1, 3])
    if a.button("Validate", key=f"grp-validate-{key}"):
        st.session_state[state_key] = (values, service.check_group(
            group_name=gname, data_product_code=dp["data_product_code"], group_code=g["group_code"] if g else None))
    checked = st.session_state.get(state_key)
    passed = bool(checked and checked[0] == values and all(c["ok"] for c in checked[1]))
    if checked and checked[0] == values:
        ui.table([{"Check": c["check"], "Result": "Passed" if c["ok"] else "Failed", "Detail": c["detail"]}
                  for c in checked[1]])
    else:
        st.caption("Validate the group before saving.")
    if b.button("Save changes" if g else "Register group", type="primary", disabled=not passed,
                key=f"grp-save-{key}"):
        st.session_state.pop(state_key, None)
        if g:
            ui.act(lambda: service.update_group(user, g["group_code"], group_name=gname,
                                                data_product_code=dp["data_product_code"], description=desc,
                                                requestable=requestable), f"Group {g['group_code']} saved.")
        else:
            ui.act(lambda: ui.flash_result(service.register_group(
                user, group_name=gname, data_product_code=dp["data_product_code"], description=desc,
                requestable=requestable), "group_code", "Group {} registered."))


with tab_grp:
    ui.instructions("catalog_groups")
    groups = queries.groups()
    ui.table([{**r, "data_product_type": service.DATA_PRODUCT_TYPES[r["data_product_type"]]} for r in groups],
             ["group_code", "group_name", "data_product_code", "data_product_name", "data_product_type",
              "environment", "status", "requestable"])
    g = pick("Edit or register", groups, "grp-pick", lambda r: f"{r['group_code']} {r['group_name']}")
    active_dps = [r for r in dp_rows if r["lifecycle_status"] == "ACTIVE"]
    if g is None:
        st.markdown("**Register a group**")
        if active_dps:
            group_form(None, active_dps)
        else:
            st.info("Register a data product first.")
    else:
        st.markdown(f"**{g['group_code']} {g['group_name']}** - {g['status'].lower()}, data product "
                    f"{g['data_product_code']} {g['data_product_name']}")
        if g["status"] == "ACTIVE":
            group_form(g, dp_rows)  # all data products, so a retired one shows up (and fails validation)
        members = queries.group_members(g["group_code"])
        st.markdown(f"**Members ({len(members)})**")
        ui.table(members, ["user_email", "status", "valid_to", "provisioned_by", "last_recertified_at",
                           "request_ref"])
        active_members = [m for m in members if m["status"] == "ACTIVE"]
        if active_members:
            with st.form("member-revoke"):
                m = st.selectbox("Remove a member", active_members, format_func=lambda r: r["user_email"])
                why = st.text_input("Reason", key="member-why")
                if st.form_submit_button("Remove member"):
                    ui.act(lambda: service.revoke_grant(user, m["grant_id"], why or None),
                           "Listed for removal on the Fulfilment page.")
        if g["status"] == "ACTIVE":
            with st.form("retire"):
                reason = st.text_input("Reason for retiring this group")
                if st.form_submit_button("Retire group (lists every member for removal)"):
                    ui.act(lambda: service.retire_group(user, g["group_code"], reason), "Group is being retired.")
