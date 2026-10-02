"""Daily: expiry, notices, SLA escalation, leavers/movers, group retirement."""
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap, graph_provider

args = bootstrap()
from cdp_security import lifecycle  # noqa: E402

print("expired:", lifecycle.expire_grants())
print("expiry notices:", lifecycle.send_expiry_notices())
print("escalations:", lifecycle.escalate_overdue_tasks())
graph = graph_provider(args)
if graph is not None:
    print("joiners/movers/leavers:", lifecycle.process_leavers_and_movers(graph))
else:
    print("JML skipped: Graph not configured")
print("groups retired:", lifecycle.finalise_retired_groups())
