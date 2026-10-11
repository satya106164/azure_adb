"""Email hand-off to the Logic App: links, payload, and what happens on success and failure."""
import pytest

from cdp_security import dispatch

APP, ADMIN = "https://app.example", "https://admin.example/"


def test_approvers_land_on_the_request():
    assert dispatch.link("APPROVAL_NEEDED", "SAR-0000123", APP, ADMIN) == \
        "https://app.example/approvals?request=SAR-0000123"
    assert dispatch.link("TASK_ESCALATED", None, APP, ADMIN) == "https://app.example/approvals"


def test_other_links():
    assert dispatch.link("ADD_MEMBER", "SAR-1", APP, ADMIN) == "https://admin.example"
    assert dispatch.link("RECERT_REVIEW", None, APP, ADMIN) == "https://app.example/recertification"
    assert dispatch.link("REQUEST_APPROVED", "SAR-1", APP, ADMIN) == "https://app.example"
    assert dispatch.link("APPROVAL_NEEDED", "SAR-1", None, ADMIN) is None


def test_payload():
    row = {"notification_id": "n1", "recipients": "a@x.com,b@x.com", "subject": "S", "body": "B\nC",
           "template": "APPROVAL_NEEDED", "request_ref": "SAR-0000009"}
    assert dispatch.payload(row, APP, ADMIN) == {
        "notification_id": "n1", "to": ["a@x.com", "b@x.com"], "subject": "S", "body": "B\nC",
        "link": "https://app.example/approvals?request=SAR-0000009", "template": "APPROVAL_NEEDED",
        "request_ref": "SAR-0000009"}


class FakeOutbox:
    """Stands in for the warehouse: one queued row, records the final status update."""

    def __init__(self, attempts=0):
        self.row = {"notification_id": "n1", "recipients": "a@x.com", "subject": "S", "body": "B",
                    "template": "REQUEST_APPROVED", "request_ref": "SAR-1", "attempts": attempts}
        self.final = None

    def fetch_all(self, sql, params=None):
        return [dict(self.row)]

    def execute(self, sql, params=None):
        if "attempts = attempts + 1" in sql:
            return 1
        self.final = params
        return 1


@pytest.fixture
def outbox(monkeypatch):
    def make(attempts=0):
        box = FakeOutbox(attempts)
        monkeypatch.setattr(dispatch, "fetch_all", box.fetch_all)
        monkeypatch.setattr(dispatch, "execute", box.execute)
        return box
    return make


class Sender:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def send(self, payload):
        if self.fail:
            raise RuntimeError("Logic App returned 500")
        self.sent.append(payload)


def test_sent_when_the_logic_app_accepts(outbox):
    box, sender = outbox(), Sender()
    assert dispatch.dispatch(sender, dry_run=False) == {"sent": 1, "failed": 0}
    assert sender.sent[0]["to"] == ["a@x.com"] and box.final["s"] == "SENT"


def test_retried_then_dead(outbox):
    box = outbox(attempts=0)
    dispatch.dispatch(Sender(fail=True), dry_run=False)
    assert box.final["s"] == "FAILED" and "500" in box.final["err"]
    box = outbox(attempts=dispatch.MAX_ATTEMPTS - 1)
    dispatch.dispatch(Sender(fail=True), dry_run=False)
    assert box.final["s"] == "DEAD"


def test_not_configured_fails_and_dry_run_sends_nothing(outbox):
    box = outbox()
    dispatch.dispatch(None, dry_run=False)
    assert box.final["s"] == "FAILED" and "logic-app-email-url" in box.final["err"]
    box, sender = outbox(), Sender()
    dispatch.dispatch(sender, dry_run=True)
    assert box.final["s"] == "SENT" and sender.sent == []
