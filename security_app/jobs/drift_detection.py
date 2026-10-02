"""Daily: compare desired grants/memberships with Unity Catalog, audit logs and Entra."""
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap, graph_provider, provisioning_principal, spark_session

args = bootstrap([("--remediate-missing", {"default": "false"})])
from cdp_security import drift  # noqa: E402

print(drift.run(spark_session(), graph_provider(args), provisioning_principal=provisioning_principal(),
                remediate_missing=args.remediate_missing.lower() == "true"))
