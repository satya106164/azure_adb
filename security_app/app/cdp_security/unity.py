"""Unity Catalog catalogs and schemas, for picking a data product's location. Read-only.

Listed as the App's service principal, which sees only catalogs it holds a privilege on:
a metastore admin grants it BROWSE on the catalogs to offer (sql/uc/001_platform_grants.sql).
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Catalog name prefixes for each environment, e.g. dev_gold, test_silver, preprod_gold, prd_gold
ENV_PREFIXES = {"dev": ("dev",), "tst": ("tst", "test"), "pre": ("pre", "preprod"), "prd": ("prd", "prod")}


def for_environment(catalogs: list[str], environment: str) -> list[str]:
    """Catalogs whose name starts with one of the environment's prefixes, followed by _ or -."""
    prefixes = "|".join(ENV_PREFIXES.get(environment, (environment,)))
    pattern = re.compile(rf"^({prefixes})[_-]")
    return [c for c in catalogs if pattern.match(c)]


def catalogs() -> list[str]:
    """Catalog names, sorted; empty if Unity Catalog cannot be read."""
    try:
        from databricks.sdk import WorkspaceClient

        return sorted(c.name for c in WorkspaceClient().catalogs.list() if c.name)
    except Exception as exc:  # the form still accepts a typed name
        log.warning("Could not list catalogs: %s", exc)
        return []


def schemas(catalog: str) -> list[str]:
    """Schema names in a catalog, sorted, without information_schema; empty if it cannot be read."""
    try:
        from databricks.sdk import WorkspaceClient

        return sorted(s.name for s in WorkspaceClient().schemas.list(catalog_name=catalog)
                      if s.name and s.name != "information_schema")
    except Exception as exc:
        log.warning("Could not list schemas in %s: %s", catalog, exc)
        return []
