# Email through an Azure Logic App

The notify job (`jobs/notify.py`, every 15 minutes in tst / prd) posts each queued email from
`notification_outbox` to a Logic App, which sends it. Nobody approves in the email: the button
opens the app, Entra single sign-on logs the person in, and they decide there (so the audit trail
records who decided).

## What the Logic App receives

`POST` to the HTTP trigger, `Content-Type: application/json`:

```json
{
  "notification_id": "6f1c2e0a-...",
  "to": ["owner@company.com", "approver2@company.com"],
  "subject": "[DEV] Approval needed: SAR-0000123",
  "body": "Reference: SAR-0000123\nAccess: Member contributions (Power BI) - group grp-...\n...",
  "link": "https://cdp-security-dev-....azure.databricksapps.com/approvals?request=SAR-0000123",
  "template": "APPROVAL_NEEDED",
  "request_ref": "SAR-0000123"
}
```

| Field | Notes |
|---|---|
| `body` | Plain text with `\n` line breaks. It contains what people typed (the justification), so escape it before putting it into HTML. |
| `link` | Where the button goes: the request on the Approvals page for approvers (`APPROVAL_NEEDED`, `TASK_ESCALATED`), Access reviews for reviewers, Catalog admin for admin emails, otherwise My access. May be `null` if the app URL is not configured. |
| `notification_id` | Unique per email. A retry after a timeout sends the same id again; ignore repeats if you need exactly-once. |
| `template` | The kind of email, e.g. `APPROVAL_NEEDED`, `REQUEST_APPROVED`, `ACCESS_GRANTED`, `ADD_MEMBER`. |

Reply with any **2xx** once the email is sent. Anything else (or no reply within 30 seconds) marks the
email `FAILED`; it is retried on the next run and marked `DEAD` after 5 attempts. Failed and dead
emails are listed on the Fulfilment page of Catalog admin.

## Reference workflow

`email_workflow.json` is a Consumption Logic App definition: HTTP trigger with the schema above, an
HTML body (escaped text, line breaks, a navy "Review this request" button), Office 365 Outlook
**Send an email (V2)**, then **Response 200**. Replace the `<subscription-id>`, `<resource-group>` and
`<region>` placeholders and authorise the `office365` connection with the mailbox the emails come from.

## Connect it to the app

1. Copy the trigger's HTTP POST URL (it includes a `sig=` signature: treat it as a secret).
2. Store it: `databricks secrets put-secret cdp_security logic-app-email-url -p <profile>`
3. Set `dry_run: "false"` for the target in `databricks.yml` (dev stays `"true"`: emails are only logged).
4. `databricks bundle deploy -t <target>` and restart the apps.
