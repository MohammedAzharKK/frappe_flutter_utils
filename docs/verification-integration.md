# Verification module: integration and operations

The shared module implements the design in [verification-module-plan.md](verification-module-plan.md).
It has no Masar, Xealth, ERPNext, or HRMS dependency. Existing app flows are not
migrated by installing this module.

## Installation

On a **disposable test site first**, migrate after updating `flutter_utils`:

```sh
bench --site <test-site> migrate
```

Migration syncs the six DocTypes and the idempotent after-migrate hook creates
`Verification Reviewer` and the default document catalog. Fresh installation runs
the same seed hook. Existing document-type configuration is never overwritten.
Enable workers and the scheduler for notification delivery and expiry processing.

The module uses Frappe 16's `extend_doctype_class` hook to prevent automatic emails
from its own Notification Log inserts; other notification producers are unchanged.

## Built-in personal verification

Use `source_app = "flutter_utils"`, `reference_doctype = "User"`, and a valid User
name. Purposes are `KYC`, `Profile`, and `Credentials`.

- Users create/edit their own submissions.
- System Manager and Verification Reviewer can review other users' submissions.
- A submitter cannot review their own submission, even if they have a reviewer role.
- KYC requests require one valid, approved National ID. Other built-in purposes
  require at least one valid, approved document.
- Submission/rework notifications reach the applicant, submitter, and assigned
  reviewer, or the global reviewer pool when no reviewer is assigned.
- In-app delivery is always enabled; built-in push and email are off. An app policy
  can enable them for its client.

## Register another app

In the consuming app's `hooks.py`:

```python
verification_policies = {
	"my_app": "my_app.verification.OrganizationVerificationPolicy",
}
```

The registered class must subclass
`flutter_utils.verification.policies.VerificationPolicy`. Registration is trusted
server configuration; the API accepts an integration identifier, never a class path.
Ambiguous registrations are rejected.

Example skeleton (replace the permission/recipient implementations with real
organization-scoped rules before enabling it):

```python
from typing import Any

from flutter_utils.verification.policies import VerificationPolicy


class OrganizationVerificationPolicy(VerificationPolicy):
	reference_doctypes = ("My Organization",)
	purposes = ("Registration",)
	push_app = "my_mobile_client"  # Device registration app identifier, not necessarily source_app.
	email_enabled = False

	def can(self, doc: Any, action: str, user: str) -> bool:
		# Verify membership, tenant scope, and operation-specific authority.
		return False

	def document_types(self, doc: Any) -> list[str]:
		return ["Business Registration", "Government Authorization"]

	def required_documents(self, request: Any) -> dict[str, int]:
		return {"Business Registration": 1}

	def tenant(self, doc: Any) -> tuple[str, str]:
		return "My Organization", doc.reference_name

	def recipients(self, doc: Any, action: str) -> list[str]:
		# Return authorized, responsible users and review-team users.
		return []

	def link(self, doc: Any, user: str) -> str:
		return f"/verification/{doc.name}"
```

### Policy contract

| Method / attribute | Responsibility |
| --- | --- |
| `reference_doctypes`, `purposes` | Explicit subject and purpose allowlists |
| `validate_subject(doc)` | Subject existence plus any app-specific restrictions |
| `tenant(doc)` | Resolve tenant from trusted records, not client input |
| `contact(doc)` | Optional responsible User; never grants access |
| `retention_until(doc)` | Optional retention date; no automatic purge |
| `can(doc, action, user)` | Read, create, edit, review, history_internal, assign, withdraw, archive |
| `query_condition(doctype, user)` | Escaped SQL condition for raw internal Desk lists |
| `list_filters(user)` | Exact readable API listing scope, or require an explicit subject |
| `document_types(doc)` | Allowed enabled document types for uploads/edits |
| `required_documents(request)` | Minimum valid approved evidence counts by type |
| `validate_document(doc)` | Additional metadata/evidence validation |
| `recipients(doc, action)` | Authorized candidate recipients for each event |
| `channels(doc, action, user)` | Optional push/email preferences per action and recipient; in-app is mandatory |
| `link(doc, user)` | Safe authenticated client route; no signed evidence links |
| `push_app`, `email_enabled` | Optional channels |
| `after_transition(doc, event)` | Transactional local business effects |

`can` and recipient resolution must handle in-memory creation contexts as well as
persisted records. Do not rely on every subject having `user`, `user_id`, or `company`.
Explicitly check Guest, disabled users, and tenant access. The engine rejects Guest
and filters disabled/unreadable recipients independently.

`query_condition` must be at least as restrictive as `history_internal`, because
Desk/raw REST records include internal fields. The default denies all rows. Grant
reviewer roles appropriate native read permissions when exposing custom reviewers
in Desk. Applicants use the safe serialized APIs, not raw DocType reads.

Native list conditions must also enforce current subject existence and tenant
access, not just compare a stale stored tenant ID. Always use the supplied `user`
in permission/recipient checks; background delivery does not run as that recipient.

`after_transition` runs inside the action transaction. Throwing rolls back the
action. Do not perform remote work or commit inside it: persist local intent and
enqueue after commit. Creating circular transitions is unsupported.

Tenant resolution runs before creation permission checks. A persisted record whose
subject has moved to a different tenant fails closed for reads, downloads, and
delivery until an authorized server-side migration reconciles that scope.

App document validators run after evidence staging and again before approval. They
can require evidence roles without blocking the temporary initial insert needed to
attach files. Tightening metadata requirements does not prevent rejection,
revocation, withdrawal, or archival of existing records.

The default notification link is a Desk route for internal review. External clients
must register a policy with their own detail route; no generic applicant portal is
included in this backend module.

Links may be a single-slash relative path or an absolute HTTPS URL without embedded
credentials. Executable schemes, protocol-relative links, and private-file/API
download routes are rejected. Use an absolute frontend URL when enabling email;
relative email links resolve against the backend site.

## API usage

All endpoints are under `flutter_utils.api.verification`, authenticated through the
existing session/managed-device mechanisms. Mutations are POST-only. Frappe wraps
returned payloads in `message`.

Create a request with `save_request(data, idempotency_key)` or create a standalone
document with `save_document(data, idempotency_key, evidence_roles)`:

```json
{
  "title": "Identity document",
  "document_type": "National ID",
  "source_app": "flutter_utils",
  "reference_doctype": "User",
  "reference_name": "applicant@example.com",
  "verification_purpose": "KYC",
  "document_number": "optional",
  "verification_request": "optional-request-id"
}
```

For uploads, send multipart form data:

- `data`: JSON object string.
- `idempotency_key`: stable random key per creation action (maximum 128 characters).
- `files`: repeated binary upload fields.
- `evidence_roles`: JSON array matching upload order, such as `["Front", "Back"]`.

Evidence supports PDF, PNG, JPG, JPEG, and MP4. Type configuration can narrow this list,
not enable arbitrary executable formats. Upload streams are size-bounded and file
signatures are checked in addition to native File validation.

MP4 evidence requires a structurally valid ISO-BMFF container with media data and
a supported video track; audio-only MP4 is rejected. This is container validation,
not full decoding, malware scanning, liveness, or identity analysis. The generic
server-only `owned_private_upload(file_id)` adapter imports fresh, unattached local
private uploads owned by the current user through the same bounded upload service.
It never reparents arbitrary existing files or accepts remote URLs.

Creation is idempotent per caller, record type, and key. Reusing a key for a different
subject/context is rejected. A concurrent duplicate may require retrying the same
key after the first transaction completes. Replays return the current record; they
do not apply new metadata/uploads.

Updates use `document_id`/`request_id` and the last returned `expected_revision`.
Subject, integration, request association, contacts, tenant, review, and audit
fields cannot be reassigned by clients. Evidence replacement creates a new evidence
revision; older files remain in history. Metadata-only changes carry unchanged
evidence into the new revision. Decisions/assignment also advance the concurrency
revision without replacing file bytes.

### Endpoints

- Configuration: `get_document_types`, `get_upload_config`.
- Documents: `get_documents`, `get_document`, `save_document`.
- Document decisions: `approve_document`, `reject_document`, `revoke_document`,
  `reset_document`, `resubmit_document`.
- Document removal: `withdraw_document`, `archive_document`.
- Requests: `get_requests`, `get_request`, `save_request`, `review_request`,
  `withdraw_request`, `archive_request`.
- Assignment: `assign_reviewer(record_doctype, record_id, reviewer, expected_revision)`.
- History: `get_review_history`, `get_evidence_history`.
- Evidence: `download_evidence(file_id)`.

`review_request` accepts `Approved`, `Rejected`, `Revoked`, `Reset`, or `Resubmitted`;
the same permissions/state machine as documents applies. Save creates/submits a
Pending request; Resubmitted returns a rejected request to Pending.

Listings return `{items, next_offset}` and default to the built-in integration.
Pass `source_app` for another integration. Scope is applied before pagination so
unrelated subjects do not leak through offsets or counts. Custom policies must
provide exact readable `list_filters`, or callers must specify the subject pair
and optional `verification_purpose` (defaulting to that policy's first purpose).
Page size is capped at 50; there is no unfiltered total.
History is also capped at 50 per page. Offset pagination can shift under concurrent
inserts; clients should deduplicate IDs and restart pagination when refreshing.

Read/save/review payloads use one serializer, including `document_revision`,
`allowed_actions`, safe current `files`, and approval/validity state. Request payloads
also include `missing_requirements` and `ready_for_approval`. Approval requires all
configured required documents; no document decision alone approves the request.
Invalid required evidence returns an approved request to Pending with an audit event.

Files receive signed URLs using file IDs and a dedicated download route. The route
still requires authentication and rechecks subject access; web clients should fetch
with authenticated headers and open a blob rather than assuming browser navigation
carries tokens. Missing evidence returns `url: null`/`available: false`, not a raw
private path. Signed URLs are not stored in notifications.

## Notifications and realtime

Each semantic action persists one immutable event and channel-specific outbox rows
in the same transaction. No eligible recipients is recorded on the event. Transport
dispatch happens after commit and is recovered by the five-minute scheduler.

- In-app uses Notification Log `subject` and `email_content`, with escaped details.
- Push reuses managed-device transport and restricts devices to `push_app`. It uses
  a privacy-safe body and event/record identifiers, not rejection reasons or IDs.
- Email is explicitly enabled per policy and handed to Frappe's Email Queue.
- Recipient permissions are rechecked before delivery.

Delivery status `Delivered` means persisted in-app, accepted by FCM, or queued for
email; it is not a guarantee that a person/device read the message. Push/email can
duplicate if a worker crashes after external acceptance but before committing.
Persistent in-app logs are deduplicated with deterministic IDs and row locks.

Failures record exception class only, attempt count, and exponential retry time.
Automatic retries stop after 10 attempts. Operators can inspect/retry exhausted
rows through trusted server code; there is no public delivery-management endpoint.
Push disabled/no devices and recipients who lost access are marked Skipped.

Listen on the authenticated user's socket for `verification_update`, `doc_update`,
`list_update`, and `notification`; refetch through the safe APIs. Verification
record updates intentionally do not broadcast cross-tenant names to shared
doctype rooms. Policies should include all relevant reviewers in their recipient
resolution so their queues receive updates.

The daily validity task handles expiry warnings and request invalidation without
rewriting historical approval or purging evidence. Reads compute validity live,
even before the scheduler runs. A request can remain historically Approved until
reevaluation, but `missing_requirements`/`ready_for_approval` expose unusable evidence
immediately.

## Testing

Database-independent tests from the bench directory:

```sh
env/bin/python -m unittest flutter_utils.tests.test_verification -v
```

Native ORM/file/permission/mixin tests on a migrated disposable site:

```sh
bench --site <test-site> run-tests --app flutter_utils --module flutter_utils.tests.test_verification_integration
```

The native tests skip without an initialized site. Do not use a production site.
They roll back their database/file effects and never migrate a site automatically.

## Security and extension limits

- All canonical mutations use the service; ordinary Desk saves, direct ORM saves,
  `db_set`, deletions, and renaming are blocked.
- File hooks prevent normal editing/detaching/publicizing/deleting managed evidence.
- Frappe's Administrator and trusted server code retain framework-level privileged
  access; raw SQL/database writes are outside the module's permission boundary.
- Raw Desk/REST reads are for authorized internal reviewers. Applicant serialization
  excludes confidential notes, audit actors, recipients, and stored file paths.
- No normal permanent-delete or automatic-purge endpoint is provided.
- Multi-stage approvals, automated KYC/OCR/biometrics, shared multi-subject evidence,
  and consuming-app migrations remain separate work.
