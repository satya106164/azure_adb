"""Every 15 minutes (and on demand from the App): apply desired state."""
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap, graph_provider, secret, spark_session

args = bootstrap()
from databricks.sdk import WorkspaceClient  # noqa: E402

from cdp_security import engine, lifecycle  # noqa: E402
from cdp_security.providers.powerbi import PowerBIProvider  # noqa: E402
from cdp_security.providers.uc_grants import UCGrantProvider  # noqa: E402
from cdp_security.providers.workspace_acl import WorkspaceAclProvider  # noqa: E402

dry_run = args.dry_run.lower() == "true"
home = WorkspaceClient()
_clients = {home.config.host.rstrip("/"): home}


def client_for(host: str) -> WorkspaceClient:
    """Same service principal, other workspaces (OAuth M2M with a Databricks-managed secret)."""
    host = host.rstrip("/")
    if host not in _clients:
        _clients[host] = WorkspaceClient(host=host, client_id=secret(args.secret_scope, "dbx-sp-client-id"),
                                         client_secret=secret(args.secret_scope, "dbx-sp-client-secret"))
    return _clients[host]


pbi = None
if not dry_run and secret(args.secret_scope, "pbi-client-id", required=False):
    pbi = PowerBIProvider(secret(args.secret_scope, "azure-tenant-id"), secret(args.secret_scope, "pbi-client-id"),
                          secret(args.secret_scope, "pbi-client-secret"))

providers = engine.Providers(
    graph=None if dry_run else graph_provider(args),
    uc=UCGrantProvider(spark_session()),
    ws_acl=WorkspaceAclProvider(client_for),
    pbi=pbi,
    dry_run=dry_run,
)
# Expire due grants here as well as in the daily job, so 4-hour break-glass access (FR-44)
# is removed within one provisioning cycle rather than at the next 06:30 run.
print("expired:", lifecycle.expire_grants())
stats = engine.run(providers)
if stats["failed"]:
    print(f"WARNING: {stats['failed']} action(s) failed; see Operations page")
