"""Microsoft Graph adapter for Entra ID security groups and memberships.

App registration needs application permissions (admin consented):
  GroupMember.ReadWrite.All, User.Read.All, and Group.Create if the app creates groups,
  plus Mail.Send (restricted with an application access policy to the sender mailbox).
"""
from __future__ import annotations

import logging

from ..naming import mail_nickname
from .base import PermanentError, http

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

    # ---- groups ------------------------------------------------------------
    def find_group(self, display_name: str) -> str | None:
        resp = http("GET", f"{GRAPH}/groups?$filter=displayName eq '{self._q(display_name)}'&$select=id",
                    self.token)
        values = resp.json().get("value", [])
        if len(values) > 1:
            raise PermanentError(f"More than one Entra group is named {display_name}")
        return values[0]["id"] if values else None

    def create_security_group(self, display_name: str, description: str, owner_ids: list[str]) -> str:
        existing = self.find_group(display_name)
        if existing:
            return existing  # idempotent
        body = {
            "displayName": display_name,
            "description": (description or display_name)[:1024],
            "mailEnabled": False,
            "mailNickname": mail_nickname(display_name),
            "securityEnabled": True,
        }
        if owner_ids:
            body["owners@odata.bind"] = [f"{GRAPH}/users/{oid}" for oid in owner_ids]
        return http("POST", f"{GRAPH}/groups", self.token, json=body).json()["id"]

    def add_member(self, group_id: str, user_id: str) -> None:
        resp = http("POST", f"{GRAPH}/groups/{group_id}/members/$ref", self.token, tolerate=(400,),
                    json={"@odata.id": f"{GRAPH}/directoryObjects/{user_id}"})
        if resp.status_code == 400 and "already exist" not in resp.text:
            raise PermanentError(f"Add member failed: {resp.text[:300]}")

    def remove_member(self, group_id: str, user_id: str) -> None:
        http("DELETE", f"{GRAPH}/groups/{group_id}/members/{user_id}/$ref", self.token, tolerate=(404,))

    def list_user_members(self, group_id: str) -> list[dict]:
        url = (f"{GRAPH}/groups/{group_id}/members/microsoft.graph.user"
               "?$select=id,mail,userPrincipalName&$top=999")
        out: list[dict] = []
        while url:
            data = http("GET", url, self.token).json()
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return out

    # ---- mail ----------------------------------------------------------------
    def send_mail(self, sender: str, recipients: list[str], subject: str, body: str) -> None:
        http("POST", f"{GRAPH}/users/{sender}/sendMail", self.token, json={
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
            },
            "saveToSentItems": False,
        })
