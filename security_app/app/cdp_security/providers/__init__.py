"""Adapters to the systems the app changes: Entra ID (Graph), Unity Catalog,
Databricks workspace object ACLs (Genie, serving endpoints, dashboards) and Power BI.
They are only used by jobs, never by the App process."""
from .base import DeferError, PermanentError, ProviderError  # noqa: F401
