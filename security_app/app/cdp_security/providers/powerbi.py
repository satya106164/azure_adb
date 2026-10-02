"""Power BI workspace and semantic model access for Entra groups (REST API).

Requires: a service principal allowed to use Power BI APIs (tenant setting),
added as Admin of each governed Power BI workspace.
"""
from __future__ import annotations

from urllib.parse import quote

from .base import PermanentError, http

PBI = "https://api.powerbi.com/v1.0/myorg"


class PowerBIProvider:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        from azure.identity import ClientSecretCredential

        self._cred = ClientSecretCredential(tenant_id, client_id, client_secret)

    def token(self) -> str:
        return self._cred.get_token("https://analysis.windows.net/powerbi/api/.default").token

    @staticmethod
    def _split(asset_type: str, external_id: str) -> tuple[str, str | None]:
        if asset_type == "PBI_WORKSPACE":
            return external_id, None
        if asset_type == "PBI_SEMANTIC_MODEL" and "/" in external_id:
            ws, ds = external_id.split("/", 1)
            return ws, ds
        raise PermanentError(f"Bad Power BI external id {external_id!r} for {asset_type}")

    def grant(self, asset_type: str, external_id: str, entra_group_id: str, privilege: str) -> None:
        ws, ds = self._split(asset_type, external_id)
        if ds is None:
            # 409/400 when already present -> make idempotent by updating instead
            resp = http("POST", f"{PBI}/groups/{ws}/users", self.token, tolerate=(400, 409), json={
                "identifier": entra_group_id, "principalType": "Group", "groupUserAccessRight": privilege})
            if resp.status_code in (400, 409):
                http("PUT", f"{PBI}/groups/{ws}/users", self.token, json={
                    "identifier": entra_group_id, "principalType": "Group", "groupUserAccessRight": privilege})
        else:
            http("POST", f"{PBI}/groups/{ws}/datasets/{ds}/users", self.token, json={
                "identifier": entra_group_id, "principalType": "Group", "datasetUserAccessRight": privilege})

    def revoke(self, asset_type: str, external_id: str, entra_group_id: str, privilege: str) -> None:
        ws, ds = self._split(asset_type, external_id)
        if ds is None:
            http("DELETE", f"{PBI}/groups/{ws}/users/{quote(entra_group_id)}?isGroup=true", self.token,
                 tolerate=(404,))
        else:
            http("PUT", f"{PBI}/groups/{ws}/datasets/{ds}/users", self.token, json={
                "identifier": entra_group_id, "principalType": "Group", "datasetUserAccessRight": "None"})
