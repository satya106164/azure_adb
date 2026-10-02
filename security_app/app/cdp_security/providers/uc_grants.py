"""Unity Catalog grants, executed with Spark SQL by the provisioning job's
run-as service principal (which needs MANAGE on the governed catalogs)."""
from __future__ import annotations

import logging

from ..ucsql import build_grant_sql
from .base import DeferError, PermanentError

log = logging.getLogger(__name__)

_NOT_SYNCED = ("PRINCIPAL_DOES_NOT_EXIST", "Could not find principal", "principal does not exist")


class UCGrantProvider:
    def __init__(self, spark):
        self.spark = spark

    def _run(self, sql: str) -> None:
        log.info("UC: %s", sql)
        try:
            self.spark.sql(sql)
        except Exception as exc:  # pyspark AnalysisException etc.
            msg = str(exc)
            if any(m.lower() in msg.lower() for m in _NOT_SYNCED):
                raise DeferError("Group not yet visible in Databricks (identity sync pending)") from exc
            if "PERMISSION_DENIED" in msg:
                raise PermanentError(f"Provisioning principal lacks MANAGE: {msg[:300]}") from exc
            raise

    def grant(self, securable_type: str, full_name: str, privilege: str, group_name: str) -> None:
        self._run(build_grant_sql("GRANT", securable_type, full_name, privilege, group_name))

    def revoke(self, securable_type: str, full_name: str, privilege: str, group_name: str) -> None:
        self._run(build_grant_sql("REVOKE", securable_type, full_name, privilege, group_name))
