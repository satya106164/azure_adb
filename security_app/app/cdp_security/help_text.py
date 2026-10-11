"""The instructions shown at the top of each page. Edit the wording here; pages only look it up by key.

Each entry has a one-line `summary` and the numbered `steps`; `note` (optional) is shown under the steps.
"""
from __future__ import annotations

PAGES: dict[str, dict] = {
    # ---- Data access app ---------------------------------------------------------------
    "my_access": {
        "summary": "See what you can access, follow your requests and manage your approval cover.",
        "steps": [
            "**What you can access** lists your access. Open an item to see its group and end date.",
            "Need it for longer? Choose **Request extension**, say why, and your approvers decide.",
            "No longer need it? Choose **Give up this access**. An admin removes you and you are told when it is done.",
            "**My requests** shows every request you raised or that is for you, and its status. You can cancel "
            "one while it is still waiting for approval.",
            "Approvers going on leave: add a delegate under **Approval cover while you are away** (up to 60 days).",
        ],
        "note": "Statuses: Pending approval, Approved (waiting for an admin to add you), Fulfilled (you have "
                "access), Rejected or Cancelled.",
    },
    "request_access": {
        "summary": "Ask for access to a data product. Its approvers decide, then an admin adds you to the group.",
        "steps": [
            "Choose the **data product**. The page shows its group and who approves it.",
            "Requesting for a colleague? Tick **I am requesting this for someone else** and pick them.",
            "Choose the **Access until** date.",
            "Write a **business justification** (at least 20 characters): what the access is for.",
            "Choose **Submit request**. You can follow it on My access.",
        ],
        "note": "You cannot request access you already have or have already asked for. To keep access for "
                "longer, use Request extension on My access instead.",
    },
    "approvals": {
        "summary": "Decide the requests waiting for you as an approver of a data product (or as a delegate).",
        "steps": [
            "Each card shows who asked, for whom, the group, the justification and the requested period.",
            "To **approve**, choose Approve. A comment is optional.",
            "To **reject**, write a comment saying why (at least 5 characters), then choose Reject.",
            "Either approver of the data product can decide. Once one has, the request leaves both lists.",
            "Open **My recent decisions** to see what you decided.",
        ],
        "note": "You never see requests you raised or that give access to you. Requests not decided by the "
                "due date are escalated, and are closed if they stay undecided.",
    },
    "recertification": {
        "summary": "Confirm who still needs access to the data products you approve.",
        "steps": [
            "Each row is one person's access, with its end date and when it was last reviewed.",
            "In **Decision**, choose **Keep** or **Remove**. Leave **Not reviewed** to come back later.",
            "To remove access, add a comment saying why.",
            "Choose **Save decisions**.",
        ],
        "note": "Anything not reviewed by the due date is listed for removal.",
    },
    "my_data_products": {
        "summary": "The data products you approve, and who has access to them now.",
        "steps": [
            "The first table lists your data products and whether you are the owner / approver or approver 2.",
            "**Who has access** lists every live grant on them.",
            "To take someone's access away, pick them under **Remove someone's access**, give a reason and "
            "choose Remove access. An admin then removes them from the group.",
        ],
    },
    # ---- Catalog admin app -------------------------------------------------------------
    "catalog_workspaces": {
        "summary": "Register the Databricks workspaces that data products live in.",
        "steps": [
            "Choose **Register a new one**, or pick a workspace to edit.",
            "Give it a name, its type, its URL (the workspace id is read from the URL) and its environment.",
            "Choose **Register workspace**. It gets an identifier such as WS-0001.",
        ],
    },
    "catalog_data_products": {
        "summary": "Register data products and the two people who approve requests for them.",
        "steps": [
            "Choose **Register a new one**, or pick a data product to edit.",
            "Choose the **environment**. The catalog list shows that environment's catalogs; tick **Show all "
            "catalogs** to see every one. Pick the schema, or **Type a name** if it is not listed.",
            "Fill in the name, type and description.",
            "Set the **Owner / approver** and **Approver 2**: two different people. Either can approve "
            "requests for every group of this data product.",
            "Choose **Register data product** (identifier DP-0001 and so on), then add its group on the Groups tab.",
        ],
        "note": "A name only has to be unique within its environment, so register the same product once per "
                "environment. Changing an approver also moves requests already waiting to the new approver.",
    },
    "catalog_groups": {
        "summary": "Link each Entra ID group to its data product so people can request it.",
        "steps": [
            "Choose **Register a new one**, or pick a group to edit.",
            "Pick the data product and type the group's name exactly as it is in Entra ID.",
            "Choose **Validate**. Every check must pass.",
            "Choose **Register group** (identifier GRP-0001 and so on). It can be requested at once if **Open for "
            "self-service requests** is ticked.",
            "For an existing group you can also see its members, list one for removal, or retire the group.",
        ],
        "note": "The app never creates groups or grants permissions: the identity team creates the group, and "
                "you give it its permissions yourself.",
    },
    "admin_approvals": {
        "summary": "Every request waiting for its approvers. Decide one yourself when neither approver can.",
        "steps": [
            "Check the table: who asked, the data product, both approvers, and the due date. Overdue requests "
            "are marked.",
            "Pick the **request**.",
            "Write the **reason** you are deciding for the approvers, for example who you confirmed it with.",
            "Choose **Approve** or **Reject**.",
        ],
        "note": "The audit trail records the decision as yours, on behalf of the owner / approver, and both "
                "approvers are emailed. You cannot decide a request you raised or that is for you. To change "
                "who approves a data product for good, edit it on the Catalog page.",
    },
    "fulfilment": {
        "summary": "Add or remove people in Entra ID yourself, then record it here.",
        "steps": [
            "**To add**: add the person to the group in Entra ID (or Databricks), pick them here, add a note "
            "such as the change ticket, and choose **Mark as granted**. They are told they now have access.",
            "**To remove**: remove them from the group first, then pick them and choose **Mark as removed**.",
            "**Done (30 days)** shows recent changes and who made them.",
            "Check **Notifications that could not be sent** for anything to resend by hand.",
        ],
        "note": "Only record a change once you have made it. You cannot record access given to yourself: "
                "another admin must do it.",
    },
    "audit": {
        "summary": "Search the tamper-evident audit trail and download evidence.",
        "steps": [
            "**Audit trail**: set the dates and filter by person, or by a reference such as SAR-0000123.",
            "Choose **Download CSV** to export what is shown.",
            "Choose **Verify audit chain integrity** to prove nothing was changed or removed. If it reports a "
            "break, escalate to security at once.",
            "**Evidence pack**: every live grant with who approved it and who added the member. Download it for "
            "access reviews or regulator requests.",
        ],
    },
}
