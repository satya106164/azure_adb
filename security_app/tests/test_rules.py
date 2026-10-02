import pytest

from cdp_security import drift, naming, templates, ucsql
from cdp_security.drift import GrantKey
from cdp_security.models import PrivilegeLevel


# ---- naming --------------------------------------------------------------------
def test_group_name_convention():
    assert naming.group_name("prd", "DATA_PRODUCT", "member_contributions", "READ") == \
        "grp-dbx-prd-dp-member_contributions-read"
    assert naming.group_name("dev", "AI_USE_CASE", "genie_members", "USER") == "grp-dbx-dev-ai-genie_members-user"


def test_group_name_rejects_wrong_level_and_bad_code():
    with pytest.raises(naming.NamingError):
        naming.group_name("prd", "DATA_PRODUCT", "x_y", "ADMIN")
    with pytest.raises(naming.NamingError):
        naming.group_name("prd", "DATA_PRODUCT", "Bad-Code", "READ")


def test_managed_group_detection():
    assert naming.is_managed_group("grp-dbx-prd-dp-abc-read", "prd")
    assert not naming.is_managed_group("grp-dbx-dev-dp-abc-read", "prd")
    assert not naming.is_managed_group("grp-dbx-prd-app-cdp_security-admin", "prd")


# ---- UC SQL ----------------------------------------------------------------------
def test_grant_sql_is_quoted():
    sql = ucsql.build_grant_sql("GRANT", "SCHEMA", "dbanalytics.member_contributions", "select",
                                "grp-dbx-prd-dp-member_contributions-read")
    assert sql == ("GRANT SELECT ON SCHEMA `dbanalytics`.`member_contributions` "
                   "TO `grp-dbx-prd-dp-member_contributions-read`")


def test_revoke_sql():
    assert ucsql.build_grant_sql("REVOKE", "CATALOG", "dbanalytics", "USE CATALOG",
                                 "grp-dbx-prd-dp-a1-read").startswith("REVOKE USE CATALOG ON CATALOG `dbanalytics` FROM")


@pytest.mark.parametrize("priv", ["ALL PRIVILEGES", "MANAGE", "OWNERSHIP"])
def test_dangerous_privileges_blocked(priv):
    with pytest.raises(ucsql.GrantSqlError):
        ucsql.build_grant_sql("GRANT", "SCHEMA", "a.b", priv, "grp-dbx-prd-dp-a1-read")


def test_injection_blocked():
    with pytest.raises(ucsql.GrantSqlError):
        ucsql.build_grant_sql("GRANT", "SCHEMA", "a.b`; DROP TABLE x; --", "SELECT", "grp-dbx-prd-dp-a1-read")
    with pytest.raises(naming.NamingError):
        ucsql.build_grant_sql("GRANT", "SCHEMA", "a.b", "SELECT", "someone@x.com")


# ---- templates -----------------------------------------------------------------------
def test_read_template_has_no_write():
    privs = {p for _, p in templates.data_product_entitlements(PrivilegeLevel.READ)}
    assert "MODIFY" not in privs and {"USE CATALOG", "USE SCHEMA", "SELECT"} <= privs


def test_ai_user_cannot_hold_manage():
    with pytest.raises(templates.TemplateError):
        templates.validate_asset_privilege("GENIE_SPACE", "CAN_MANAGE", "USER")
    assert templates.validate_asset_privilege("GENIE_SPACE", "CAN_MANAGE", "ADMIN") == "CAN_MANAGE"


def test_ai_template_two_levels():
    assets = [("a1", "GENIE_SPACE"), ("a2", "SERVING_ENDPOINT")]
    assert templates.ai_use_case_entitlements("USER", assets) == [("a1", "CAN_RUN"), ("a2", "CAN_QUERY")]
    assert templates.ai_use_case_entitlements("ADMIN", assets)[0] == ("a1", "CAN_MANAGE")


# ---- drift -------------------------------------------------------------------------------
G = "grp-dbx-prd-dp-member-read".replace("member", "members")


def test_drift_missing_and_unexpected():
    desired = {GrantKey.of("SCHEMA", "dbanalytics.members", G, "SELECT")}
    actual = {GrantKey.of("SCHEMA", "dbanalytics.members", "grp-dbx-prd-dp-members-write", "SELECT"),
              GrantKey.of("SCHEMA", "dbanalytics.members", "jane@x.com", "SELECT"),
              GrantKey.of("SCHEMA", "dbanalytics.members", "platform-admins", "SELECT")}
    found = {f.finding_type for f in drift.compare_grants(desired, actual, env="prd")}
    assert found == {"MISSING_GRANT", "UNEXPECTED_GRANT", "DIRECT_USER_GRANT"}


def test_drift_unmanaged_principal_flagged_only_on_restricted():
    actual = {GrantKey.of("SCHEMA", "dbanalytics.members", "platform-admins", "SELECT")}
    assert drift.compare_grants(set(), actual, env="prd") == []
    out = drift.compare_grants(set(), actual, env="prd", restricted={"dbanalytics.members"})
    assert out[0].severity == "HIGH"
    assert drift.compare_grants(set(), actual, env="prd", restricted={"dbanalytics.members"},
                                allowlist=["platform-admins"]) == []


def test_drift_members():
    out = drift.compare_members("g", must_have={"a@x.com"}, may_have={"b@x.com"}, actual={"b@x.com", "c@x.com"})
    assert {(f.finding_type, f.principal) for f in out} == {("MISSING_MEMBER", "a@x.com"),
                                                            ("UNEXPECTED_MEMBER", "c@x.com")}
