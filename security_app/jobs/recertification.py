"""Daily: close overdue campaigns (revoke unattested), then add newly due grants."""
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap

args = bootstrap()
from cdp_security import recert  # noqa: E402

print("revoked (not attested):", recert.close_overdue())
print("items added:", recert.open_periodic())
