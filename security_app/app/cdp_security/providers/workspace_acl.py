"""Workspace object permissions (Genie spaces, serving endpoints incl. agent and
AI Function endpoints, AI/BI dashboards) via the Databricks Permissions API.

The provisioning SP must be a workspace user with CAN_MANAGE on each object in
every workspace registered in the app (dbanalytics, Analytics, MLops, project
workspaces).
"""
from __future__ import annotations

import logging
from typing import Callable

from .base import PermanentError

log = logging.getLogger(__name__)

OBJECT_TYPES = {
    "GENIE_SPACE": "genie",
    "SERVING_ENDPOINT": "serving-endpoints",
    "AI_FUNCTION": "serving-endpoints",
    "AGENT": "serving-endpoints",
    "DASHBOARD": "dashboards",
}


class WorkspaceAclProvider:
    def __init__(self, client_factory: Callable[[str], object]):
        self._client = client_factory

    def _target(self, w, asset_type: str, external_id: str) -> tuple[str, str]:
        object_type = OBJECT_TYPES.get(asset_type)
        if not object_type:
            raise PermanentError(f"{asset_type} is not a workspace ACL asset")
        if object_type == "serving-endpoints":
            return object_type, w.serving_endpoints.get(external_id).id  # ACLs use the endpoint id, not name
        return object_type, external_id

    def grant(self, host: str, asset_type: str, external_id: str, group_name: str, privilege: str) -> None:
        from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

        w = self._client(host)
        object_type, object_id = self._target(w, asset_type, external_id)
        log.info("ACL grant %s %s/%s -> %s", privilege, object_type, object_id, group_name)
        w.permissions.update(request_object_type=object_type, request_object_id=object_id,
                             access_control_list=[AccessControlRequest(group_name=group_name,
                                                                       permission_level=PermissionLevel(privilege))])

    def revoke(self, host: str, asset_type: str, external_id: str, group_name: str, privilege: str) -> None:
        """Rewrite the direct ACL without this group (Permissions API has no single-entry delete)."""
        from databricks.sdk.service.iam import AccessControlRequest

        w = self._client(host)
        object_type, object_id = self._target(w, asset_type, external_id)
        current = w.permissions.get(request_object_type=object_type, request_object_id=object_id)
        keep: list[AccessControlRequest] = []
        for acl in current.access_control_list or []:
            if acl.group_name == group_name:
                continue
            for perm in acl.all_permissions or []:
                if perm.inherited:
                    continue
                keep.append(AccessControlRequest(user_name=acl.user_name, group_name=acl.group_name,
                                                 service_principal_name=acl.service_principal_name,
                                                 permission_level=perm.permission_level))
        log.info("ACL revoke %s/%s from %s (%d direct entries kept)", object_type, object_id, group_name, len(keep))
        w.permissions.set(request_object_type=object_type, request_object_id=object_id, access_control_list=keep)
