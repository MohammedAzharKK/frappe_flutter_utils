# Shared Verification Module — Implementation Plan

Status: shared foundation implemented; site migration and native acceptance testing required.

See [verification-integration.md](verification-integration.md) for installation,
policy contracts, APIs, notification operations, and testing. Masar/Xealth migration
remains separate follow-up work.

## Goal

Build an abstract, reusable verification capability in `flutter_utils` for KYC,
profile verification, professional credentials, employee compliance, organization
verification, and other app-defined subjects.

The module must work without Masar, Xealth, ERPNext, or HRMS installed. Consuming
apps register policies instead of copying document models, approval logic, or
notification code. The shared engine must not contain app-name conditionals.

## Initial scope

- Canonical verification documents and configurable document types.
- Private, multi-file evidence with revisions.
- Whole-submission verification requests grouping documents for one subject.
- Approval, rejection, resubmission, revocation, reset, withdrawal, and archive.
- Server-enforced permissions, transition rules, and concurrency protection.
- Durable audit events and retryable notification delivery.
- Shared APIs and Frappe Desk review actions.
- Tests and consuming-app integration documentation.

Build and test the shared foundation first. Migrating Masar and Xealth, changing
their existing behavior, and updating their clients are separate follow-up work.

## Architecture

```text
Consuming app
    -> registers trusted verification policies
flutter_utils verification service
    -> documents, requests, evidence, and revisions
    -> permission checks and state transitions
    -> audit events
    -> notification delivery and realtime updates
```

| Shared engine owns | Consuming app defines |
| --- | --- |
| Document validation and lifecycle | Allowed subjects and who can manage them |
| Evidence storage and revisions | Authorized reviewers and tenant boundaries |
| Review history and concurrency | Required evidence for each purpose |
| Notification creation and delivery | Additional recipients and client routes |
| Generic APIs and Desk actions | Business effects of approval or rejection |

For example, Masar profile visibility and federation rules remain Masar policies.
Xealth employee/company permissions and training CSV interpretation remain Xealth
policies.

## Generic subjects, not mandatory users

Both `Verification Document` and `Verification Request` require:

| Field | Frappe field type | Meaning |
| --- | --- | --- |
| `reference_doctype` | Link to DocType | Type of subject being verified |
| `reference_name` | Dynamic Link using `reference_doctype` | Subject record |

Possible subjects include User, Employee, Company, Supplier, Customer, a profile,
or any registered custom DocType. These are examples, not hard dependencies.

Do not require a `user` field. Personal verification references a User directly.
Do not infer subject ownership from Frappe's built-in `owner` field.

People performing actions still link to User through `submitted_by`,
`reviewed_by`, optional `assigned_reviewer`, and audit-event actors. An optional
`contact_user` identifies a responsible contact but does not grant access.

Default to one canonical subject per document. Requests group documents for the
same subject and integration context. Additional contextual references must not
automatically grant access. Sharing evidence across multiple subjects is deferred
until explicit association and permission rules are designed.

## Data model

### Verification Document

Source of truth for an individual document and its current approval state.

| Group | Proposed fields |
| --- | --- |
| Identity | `title`, `document_type`, `document_number`, `description` |
| Subject | `reference_doctype`, `reference_name`, optional `contact_user` |
| Integration | `source_app`, `verification_purpose`, optional `verification_request` |
| Tenant | optional `tenant_doctype`, `tenant_name` |
| Holder | optional `holder_name`, `date_of_birth`, `nationality` |
| Issuer | `issuing_authority`, `issuing_country`, `issuing_region` |
| Validity | `issue_date`, `expiry_date`, `no_expiry`, derived `validity_status` |
| Evidence | `files`, `primary_file`, `document_revision` |
| Submission | `submitted_by`, `submitted_at`, `last_resubmitted_at`, `submission_count` |
| Approval | `approval_status`, `rejection_code`, `rejection_reason` |
| Review | `reviewed_by`, `reviewed_at`, `review_notes`, optional `assigned_reviewer` |
| Retention | `is_archived`, `archived_at`, optional `retention_until` |

Most metadata is optional. Document types and app policies determine requirements.
Collect sensitive holder metadata only when needed; do not copy entire profiles
into verification records. Applicant-visible notes and confidential reviewer
notes must be separate and serialized according to permission.

Tenant references are generic so the module does not require Company. Tenant and
integration context are validated or resolved server-side, not trusted from client
input.

### Verification Document Type

Configurable document catalog rather than hardcoded Select options. Types define
required metadata, expiry expectations, allowed formats, upload limits, and
permitted evidence roles.

Suggested seed types:

- National ID, Passport, Residence Permit, Driving License.
- Proof of Address, Selfie / Identity Photo.
- Professional License, Certification, Training.
- Business Registration, Government Authorization, Affiliation.
- Insurance, Medical, Compliance, Contract, Other.

Consuming integrations determine which types are available for their purposes.

### Verification Document File

Child table for private evidence files with roles such as Front, Back, Selfie,
Supporting Document, or Additional Page. Preserve revision associations and
identify primary evidence. Changes must not overwrite previously reviewed evidence
without retaining its revision history.

### Verification Review Log

Separate, server-written audit DocType. Record an event ID, action, subject record,
document/request reference, revision, previous and new status, actor, timestamp,
reason, and integration context. Do not use the log as the current-state authority.

Audit history must not be editable through normal applicant/reviewer operations.
Avoid duplicating unnecessary sensitive evidence in event payloads.

### Verification Request

Groups documents for whole-profile/KYC review, with the same generic subject and
integration context. Tracks its own submission and review decision.

Approving one document must not automatically approve the subject or request.
Policy requirements determine whether the request is ready for approval. A change,
revocation, or expiry affecting required evidence must trigger policy evaluation
and an auditable request-state effect where appropriate.

Start with one review stage. Preserve extension points for future multi-stage
review; federation/admin or other stage-specific rules are not hardcoded.

### Notification delivery tracking

Use durable delivery/outbox records tied to audit events for reliable dispatch,
retry status, and deduplication. Final internal DocType names can be selected
during implementation.

## Lifecycle and validation

Document approval statuses remain `Pending`, `Approved`, and `Rejected`.

```text
Pending  -> Approved : authorized approval
Pending  -> Rejected : authorized rejection with reason
Rejected -> Pending  : resubmission or authorized reset
Approved -> Pending  : authorized revocation or evidence replacement
```

- New submissions default to Pending.
- Rejection and revocation require reasons.
- Evidence replacement or verification-critical metadata changes invalidate the
  previous decision and preserve history.
- All review actions check both review authority and subject/tenant access.
- Use atomic concurrency protection with expected revision/modification state;
  checking status alone is insufficient.
- Validate issue/expiry ordering and contradictory no-expiry inputs.
- Keep approval and validity separate. Expired evidence can remain historically
  approved but cannot count as currently valid evidence.
- Keep validity accurate as time passes, not only when a record is saved.
- Model withdrawal/archive separately from the three approval statuses.
- Prefer withdrawal/archive for reviewed records. Permanent deletion and evidence
  purging require an explicit authorized retention policy.
- Do not enable automatic evidence purging by default.
- Enforce rules in APIs, Desk, and document/controller operations, not only buttons.

## Evidence privacy

- Store verification evidence as private Frappe File records.
- Validate upload size, permitted content/formats, attachment ownership, and access.
- Never return raw `/private/files/...` paths to external clients.
- Reuse or extract the existing shared signing helper in
  `flutter_utils/api/banners.py` without breaking current callers.
- If safe URL generation fails, return an unavailable-evidence result, never a raw
  private-path fallback.
- Signed URLs must only be issued after authorization and must not be included in
  persistent notification payloads.
- Evidence download and review-history access follow the subject permission boundary.

## Trusted integration policies

Register server-side handlers through Frappe hooks. Define and document contracts
for these responsibilities:

1. Allowed reference DocTypes and verification purposes.
2. Subject validation and responsible-contact resolution.
3. Tenant resolution and validation.
4. Read, submit/edit, review, archive, and history permissions.
5. Required document types and metadata.
6. Reviewer assignment and notification recipients.
7. Client routes and channel preferences.
8. Business effects of transitions and required-evidence changes.

Clients cannot provide arbitrary Python paths, unrestricted recipients, or trusted
tenant assignments. Reject unregistered integrations/references by default. A
built-in user-owned policy can support personal verification without a consuming
app adapter.

Dynamic Links alone do not provide authorization. Do not assume every subject
DocType contains `user`, `user_id`, or `company` fields. Permission enforcement must
cover list queries, individual records, evidence, history, API, and Desk access.

## Notifications for lifecycle actions

Every successful meaningful lifecycle action creates an audit event and persistent
in-app notifications for authorized recipients. Reads and incidental internal
saves are not notification actions.

| Action | Applicant/contact message | Reviewer/manager message |
| --- | --- | --- |
| Submitted | Submission received | New submission awaiting review |
| Updated / evidence replaced | Update acknowledged; current review status | Revised evidence awaiting review, when applicable |
| Resubmitted | Resubmission received | Ready for another review |
| Approved | Approval confirmed | Decision recorded, as configured |
| Rejected | Reason and correction/resubmission instructions | Decision recorded, as configured |
| Revoked | Reason and next step | Review required again |
| Reset | Returned to pending review | Awaiting review |
| Withdrawn / archived | Withdrawal/archive confirmed | Review no longer required |
| Reviewer assigned / reassigned | Optional progress update | Assignment notification |
| Expiring soon | Renewal reminder | Optional compliance reminder |
| Expired | Replacement required | Optional compliance alert |

Requests emit corresponding request-level events. Avoid redundant document/request
notifications for the same user-facing action.

Subjects need not have a User contact. Policies resolve recipients from the
subject, submitter, and review team. Never invent a user association. A lack of
eligible recipients must be recorded rather than bypassing permission boundaries.

### Channels and content

- In-app: standard Frappe Notification Log, using `subject` for title and
  `email_content` for the detailed message.
- Push: configured delivery through
  `flutter_utils.push_notifications.enqueue_push_notifications` or its existing
  worker transport, retaining app-scoped device targeting.
- Email: configurable per integration/action and disabled by default.
- Realtime: authorized client updates after commit for records and notification
  badges; not a replacement for durable notification records.

Messages include a concise title, human-readable document/request title, relevant
result/reason, next step, record reference, and a policy-resolved client link.
Escape user-provided content before storing HTML. Do not expose confidential review
notes or sensitive document numbers. Push messages use privacy-safe summaries;
detailed reasons belong in authorized views or policy-approved email.

### Reliability

- Persist events/delivery intents transactionally with the action; dispatch only
  after commit. Rolled-back actions must not notify users.
- Deduplicate by event, recipient, and channel. Record delivery attempts and retry
  failures; document transport limitations rather than promising exactly-once push.
- Notification transport failures must not undo a successful review.
- Keep the originating integration/client scope for push delivery.
- A submitter receives their acknowledgement even when they performed the action.
- One semantic action can involve multiple saves but must emit only one event.

## API and Desk surfaces

Suggested public API module: `flutter_utils.api.verification`.

Document operations:

- `get_document_types`, `get_upload_config`.
- `get_documents`, `get_document`, `save_document`.
- `resubmit_document`, `approve_document`, `reject_document`.
- `revoke_document`, `reset_document`, `withdraw_document`, `archive_document`.
- `get_review_history`.

Provide corresponding request create/read/submit/review operations. Final endpoint
signatures and response envelopes must follow existing shared API conventions.

Return safe evidence URLs, revision state, and contextual `allowed_actions`.
Paginate listings and expose only authorized fields. Separate applicant mutations
from reviewer decisions; applicants cannot write status, actors, history, or tenant
fields directly.

Desk forms use the same services for review actions, prompt for mandatory reasons,
and display history and permitted actions. Read-only UI fields alone are not a
security boundary.

## Implementation sequence

1. Define policy contracts, canonical models, and permission boundaries.
2. Implement lifecycle services, revisions, private evidence, and audit events.
3. Implement request grouping and required-evidence evaluation.
4. Add durable notification delivery, shared channels, and realtime updates.
5. Add APIs and Desk review surfaces using the same services.
6. Add tests, seed document types, and integration documentation.
7. Validate standalone operation without consuming apps or ERPNext/HRMS.

Follow-up integration work, not part of this initial change:

- Thin Masar/Xealth policy adapters retaining existing public API compatibility.
- Idempotent old-ID to new-ID migration and reconciliation of histories/files.
- Updates to links, profile tables, reports, notifications, and clients.
- Per-app cutover to the shared canonical records; avoid long-term dual writes.

## Acceptance tests

- Generic User and custom-DocType subjects work without mandatory user ownership.
- Standalone installation does not import Masar, Xealth, ERPNext, or HRMS.
- Unregistered references and cross-user/tenant access are denied.
- API, Desk, list, history, evidence, and direct-save paths enforce policies.
- Applicants cannot self-approve or modify audit/reviewer fields.
- Evidence/critical metadata updates invalidate decisions and retain revisions.
- Concurrent and repeated actions cannot overwrite newer decisions or duplicate events.
- Request approval is independent from document approval and checks requirements.
- Expiry stays accurate and invalidates usable evidence without rewriting history.
- Private paths and confidential notes never leak through serialization or notifications.
- Every action has appropriate title, description, recipients, and client route.
- Failed/rolled-back actions create no delivered notifications.
- Retry handling deduplicates persistent notifications and tracks transport failures.
- Push remains app-scoped and uses privacy-safe content.
- Archive/withdrawal preserves history; no default automatic evidence purge occurs.

## Deferred capabilities

- Multi-stage/multi-party approval orchestration.
- Automated KYC provider integrations, OCR, and biometric processing.
- Shared evidence ownership across multiple canonical subjects.
- Production retention schedules and automatic evidence purging.

If automated checks are added later, store them in separate provider-check records
with explicit permissions and retention, not unrestricted payloads in generic notes.
