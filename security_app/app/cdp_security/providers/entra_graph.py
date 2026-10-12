"""Microsoft Graph adapter: look up people (leavers / movers). Email goes through the Logic App
(dispatch.py). It never changes groups or memberships; admins do that by hand.

App registration needs the application permission User.Read.All (admin consented).
"""
from __future__ import annotations

import logging

from .base import http

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"


class EntraGraphProvider:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        from azure.identity import ClientSecretCredential

        self._cred = ClientSecretCredential(tenant_id, client_id, client_secret)

    def token(self) -> str:
        return self._cred.get_token("https://graph.microsoft.com/.default").token

    @staticmethod
    def _q(value: str) -> str:
        return value.replace("'", "''")

    # ---- users -------------------------------------------------------------
    def resolve_user(self, email: str) -> dict | None:
        """Return {id, accountEnabled, department, mail, userPrincipalName, displayName} or None."""
        select = "$select=id,accountEnabled,department,mail,userPrincipalName,displayName"
        resp = http("GET", f"{GRAPH}/users/{email}?{select}", self.token, tolerate=(404,))
        if resp.status_code == 200:
            return resp.json()
        resp = http("GET", f"{GRAPH}/users?$filter=mail eq '{self._q(email)}'&{select}", self.token)
        values = resp.json().get("value", [])
        return values[0] if len(values) == 1 else None
