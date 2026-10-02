"""Best-effort: start the provisioning job now instead of waiting for its schedule.

The App service principal needs CAN_MANAGE_RUN on the provisioning job; the job
has queueing enabled so overlapping triggers are serialised, never parallel.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def trigger_provisioning() -> bool:
    job_id = os.environ.get("CDP_SECURITY_PROVISIONING_JOB_ID")
    if not job_id or job_id == "0":  # "0": not set yet (bundle default)
        return False
    try:
        from databricks.sdk import WorkspaceClient

        WorkspaceClient().jobs.run_now(job_id=int(job_id))
        return True
    except Exception as exc:  # the schedule will pick it up anyway
        log.warning("Could not trigger provisioning job: %s", exc)
        return False
