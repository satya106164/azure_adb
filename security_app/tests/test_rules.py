import pytest

from cdp_security import naming, notify, service


# ---- group names -------------------------------------------------------------------
@pytest.mark.parametrize("name", ["grp-dbx-prd-dp-member_contributions-read", "Finance Readers (PBI)",
                                  "sg.analytics-users"])
def test_group_names_accepted(name):
    assert naming.validate_group_name(f"  {name} ") == name


@pytest.mark.parametrize("name", ["", "ab", "-starts-with-dash", "two  spaces", "semi;colon", "x" * 300,
                                  "grp`; DROP TABLE x"])
def test_group_names_refused(name):
    with pytest.raises(naming.NamingError):
        naming.validate_group_name(name)


def test_suggested_group_name_follows_the_convention():
    assert naming.suggest_group_name("dev", "Member Contributions!") == "grp-dbx-dev-dp-member_contributions-read"


# ---- data products -------------------------------------------------------------------
def _dp(**kw):
    values = dict(name="Sales", description=None, data_product_type="POWER_BI", project=None, catalog=None,
                  schema=None, classification_code="GENERAL", owner_email="o@x.com", steward_email="s@x.com",
                  home_workspace_code=None)
    values.update(kw)
    return service._data_product_values(**values)


def test_schema_is_optional_except_for_the_schema_type():
    assert _dp()["catalog"] is None and _dp()["schema"] is None
    assert _dp(data_product_type="GENIE_AGENT", catalog="Main", schema="Sales")["schema"] == "sales"
    with pytest.raises(service.ServiceError, match="catalog and schema"):
        _dp(data_product_type="SCHEMA", catalog="main")
    with pytest.raises(service.ServiceError, match="catalog the schema is in"):
        _dp(schema="sales")


def test_data_product_type_and_project():
    assert set(service.DATA_PRODUCT_TYPES) == {"DATABRICKS_DASHBOARD", "POWER_BI", "GENIE_AGENT", "CUSTOM_AGENT",
                                               "DATABRICKS_APP", "SCHEMA"}
    assert _dp(project="  Pensions 2027 ")["project"] == "Pensions 2027"
    with pytest.raises(service.ServiceError, match="Type must be"):
        _dp(data_product_type="TABLE")


def test_odd_schema_names_refused():
    with pytest.raises(service.ServiceError, match="only letters"):
        _dp(data_product_type="SCHEMA", catalog="main", schema="a`; drop")


def test_workspace_types():
    assert list(service.WORKSPACE_TYPES) == ["INGESTION", "ANALYTICS_PBI", "MLOPS", "DATABRICKS_DASHBOARDS"]


def test_access_label_names_the_type():
    assert service.access_label({"data_product_name": "Sales", "data_product_type": "GENIE_AGENT"}) == \
        "Sales (Genie agent)"


def test_admin_alerts_link_to_the_admin_app(monkeypatch):
    monkeypatch.setenv("CDP_SECURITY_APP_URL", "https://req")
    monkeypatch.setenv("CDP_SECURITY_ADMIN_APP_URL", "https://adm")
    from cdp_security.config import get_settings
    get_settings.cache_clear()
    try:
        assert notify._footer(admin=True).endswith("https://adm")
        assert notify._footer().endswith("https://req")
    finally:
        get_settings.cache_clear()
