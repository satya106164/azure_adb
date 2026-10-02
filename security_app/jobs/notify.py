"""Send queued notifications."""
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap, graph_provider, secret

args = bootstrap()
from cdp_security import dispatch  # noqa: E402

dry_run = args.dry_run.lower() == "true"
teams_url = secret(args.secret_scope, "teams-security-webhook", required=False)
print(dispatch.dispatch(
    graph=None if dry_run else graph_provider(args),
    sender_mailbox=secret(args.secret_scope, "mail-sender", required=False),
    teams=dispatch.TeamsWorkflowSender(teams_url) if teams_url else None,
    dry_run=dry_run,
))
