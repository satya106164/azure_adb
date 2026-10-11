"""Send queued notifications through the Logic App (secret logic-app-email-url)."""
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap, secret

args = bootstrap()
from cdp_security import dispatch  # noqa: E402

dry_run = args.dry_run.lower() == "true"
url = None if dry_run else secret(args.secret_scope, "logic-app-email-url")
print(dispatch.dispatch(sender=dispatch.LogicAppSender(url) if url else None, dry_run=dry_run))
