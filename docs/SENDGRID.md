# SendGrid HTTP email

Flutter Utils provides SendGrid delivery for standard Frappe Email Accounts and
Email Queue records. The separate `frappe_sendgrid` app is deprecated and is not
required on new sites. Requires Frappe 16's `extend_doctype_class` support.

## Configuration

1. Install or upgrade Flutter Utils and run `bench --site <site> migrate`.
2. Run `bench build --app flutter_utils` and restart web/scheduler/worker processes.
3. Create or edit an Email Account and choose **Sendgrid HTTP**.
4. Set the email address to a sender verified with SendGrid and enable outgoing
   email. This service is outgoing-only and always uses the account sender.
5. Enter a SendGrid API key with Mail Send permission in the encrypted **Password**
   field, or retain the existing `sendgrid_api_key` site configuration fallback.
6. Set the default outgoing account as appropriate. Ensure scheduler and workers
   are running. Send an email and inspect Email Queue for status and errors.

No API key, account, service value, or pending queue migration is needed for an
existing `frappe_sendgrid` installation. Setup adds the service option without
overwriting other services or site customizations.

## Delivery behavior

- Only `Sendgrid HTTP` accounts use the integration. SMTP and Frappe Mail use
  Frappe's native sender; no global `override_email_send` hook is installed.
- HTTP requests enqueue delivery after commit, including callers using
  `frappe.sendmail(now=True)` (such as email OTP). A successful request means the
  email was queued, not that SendGrid accepted it. Workers perform the HTTP call.
- Standard Email Queue retry limits, per-recipient progress, failure notifications,
  message redaction, and queue status remain managed by Frappe's SendMailContext.
- A queue-specific Redis lock prevents concurrent worker sends from racing;
  already-sent recipients are skipped on retry.
- Provider timeouts are ambiguous: SendGrid may accept an email before the client
  times out. SendGrid Mail Send does not provide an exactly-once idempotency key;
  inspect provider activity before manually retrying to avoid duplicates.
- Text/HTML content, encoded subjects and names, Reply-To, attachments, and inline
  Content-ID attachments are supported. Envelope recipients are sent individually
  so BCC recipients are not disclosed in personalizations.
- Network failures and rejected responses are recorded through Email Queue using
  sanitized errors. Provider response bodies and API keys are not logged.
- HTTP 202 means accepted by SendGrid, not confirmed delivery. Delivery/bounce
  webhooks are not part of this integration (or the deprecated app).

## Migration from frappe_sendgrid

Deploy the Flutter Utils integration and the compatibility release of
`frappe_sendgrid` together, along with dependent apps that no longer require the
old app (`masar` and `lms_custom` in this workspace). Do not leave the old transport
hooks deployed alongside this integration. Migrate, build assets, restart, and
verify before explicitly uninstalling the old app on each site. Take a backup
first and retain the old app on the bench until all sites have migrated.

## Verification checklist

- Fresh installation exposes Sendgrid HTTP without installing frappe_sendgrid.
- Existing accounts retain passwords, sender configuration, and pending messages.
- Both apps installed: only Flutter Utils registers email extensions and assets.
- With the old app uninstalled: SendGrid continues to work.
- SMTP and Frappe Mail still send through their native paths.
- Test plain text/HTML, Unicode headers, Reply-To, attachments and inline images.
- Missing keys, rejected requests, and timeouts leave recoverable queue errors.
- Retrying partially sent mail skips recipients already marked sent.
- OTP requests enqueue without waiting for SendGrid. Delivery failures are visible
  in Email Queue and do not falsely imply provider acceptance in the API response.

Offline tests (no email sent):

```sh
env/bin/python -m unittest flutter_utils.tests.test_sendgrid_email
```
