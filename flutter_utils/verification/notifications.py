from __future__ import annotations

import hashlib
from html import escape
from typing import Any
from urllib.parse import unquote, urlsplit

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from flutter_utils.verification.model import internal_write
from flutter_utils.verification.policies import can_access, get_policy

NEXT_STEPS = {
	"Submitted": "Your submission is awaiting review.",
	"Updated": "Check the current status; changed evidence may require another review.",
	"Resubmitted": "Your corrected submission is awaiting review.",
	"Approved": "No further action is needed unless the evidence changes or expires.",
	"Rejected": "Please correct the information or evidence and resubmit.",
	"Revoked": "Please check the reason and provide updated evidence if required.",
	"Reset": "The submission is awaiting review again.",
	"Withdrawn": "This submission is no longer awaiting review.",
	"Archived": "This submission has been archived; its review history is retained.",
	"Reviewer Assigned": "The assigned reviewer should review the submission.",
	"Expiring Soon": "Please provide renewed evidence before the expiry date.",
	"Expired": "Please upload replacement evidence.",
	"Evidence Invalidated": "Required evidence is no longer valid; another review is needed.",
}


def client_link(link: str) -> str:
	"""Allow client/Desk routes, never executable schemes or evidence download links."""
	if not isinstance(link, str):
		frappe.throw("Verification notification routes must be text.")
	parts = urlsplit(link)
	relative = link.startswith("/") and not link.startswith("//") and not parts.netloc
	absolute = parts.scheme == "https" and bool(parts.netloc) and not parts.username and not parts.password
	if link and (
		not (relative or absolute) or unquote(parts.path).startswith(("/private/files/", "/api/method/"))
	):
		frappe.throw("Verification notifications require a safe client detail route.")
	return link


def message_for(doc: Any, action: str, reason: str | None, reviewer: bool = False) -> tuple[str, str]:
	label = "Verification request" if doc.doctype == "Verification Request" else "Verification document"
	subject = f"{label}: {action.lower()}"
	parts = [f'{label} "{escape(doc.title or doc.name)}": {escape(action.lower())}.']
	parts.append(f"Current approval status: {escape(doc.approval_status)}.")
	if reason:
		parts.append(f"Reason: {escape(reason)}.")
	if action in {"Expiring Soon", "Expired"} and doc.expiry_date:
		parts.append(f"Expiry date: {escape(str(doc.expiry_date))}.")
	if reviewer and action in {"Submitted", "Updated", "Resubmitted", "Reset", "Evidence Invalidated"}:
		parts.append("Please review this submission and its current evidence.")
	else:
		parts.append(NEXT_STEPS[action])
	return subject, " ".join(parts)


def _push_copy(doc: Any, event: Any) -> tuple[str, str]:
	"""Action-first push copy that never leaks the document title.

	Push content is visible on the lock screen, so it names the action and the
	next step instead of the evidence title. The full detail stays in the
	Notification Log and the in-app list.
	"""
	label = "Verification request" if doc.doctype == "Verification Request" else "Verification document"
	title = f"{label} {str(event.action).lower()}"
	body = NEXT_STEPS.get(event.action, "Open the app to view your verification update.")
	if event.reason:
		body = f"{body} Reason: {event.reason}"
	return title[:120], body[:300]


def record_event(doc: Any, action: str, before: str | None, reason: str | None = None) -> Any:
	policy = get_policy(doc.source_app)
	key = hashlib.sha256(f"{doc.doctype}:{doc.name}:{doc.document_revision}:{action}".encode()).hexdigest()
	existing = frappe.db.get_value("Verification Review Log", {"deduplication_key": key}, "name")
	if existing:
		return frappe.get_doc("Verification Review Log", existing)
	requested = list(dict.fromkeys(policy.recipients(doc, action)))
	enabled = (
		frappe.get_all("User", filters={"name": ["in", requested], "enabled": 1}, pluck="name")
		if requested
		else []
	)
	recipients = [user for user in enabled if can_access(doc, "read", user, policy)]
	with internal_write():
		event = frappe.get_doc(
			{
				"doctype": "Verification Review Log",
				"record_doctype": doc.doctype,
				"record_name": doc.name,
				"reference_doctype": doc.reference_doctype,
				"reference_name": doc.reference_name,
				"source_app": doc.source_app,
				"verification_purpose": doc.verification_purpose,
				"action": action,
				"revision": doc.document_revision,
				"status_before": before,
				"status_after": doc.approval_status,
				"actor": frappe.session.user,
				"occurred_at": now_datetime(),
				"reason": reason,
				"review_notes": doc.review_notes
				if action in {"Approved", "Rejected", "Revoked", "Reset"}
				else None,
				"notification_state": "Resolved" if recipients else "No eligible recipients",
				"deduplication_key": key,
			}
		).insert(ignore_permissions=True)
		for user in recipients:
			subject, message = message_for(doc, action, reason, reviewer=policy.can(doc, "review", user))
			optional = set(policy.channels(doc, action, user)) - {"In App"}
			if optional - {"Push", "Email"} or ("Push" in optional and not policy.push_app):
				frappe.throw("Invalid verification notification channel configuration.")
			channels = ["In App", *sorted(optional)]
			for channel in channels:
				frappe.get_doc(
					{
						"doctype": "Verification Notification Delivery",
						"event": event.name,
						"recipient": user,
						"channel": channel,
						"delivery_key": hashlib.sha256(f"{event.name}:{user}:{channel}".encode()).hexdigest(),
						"status": "Pending",
						"subject": subject,
						"message": message,
						"link": client_link(policy.link(doc, user)),
						"push_app": policy.push_app,
					}
				).insert(ignore_permissions=True)
			frappe.publish_realtime(
				"verification_update",
				{
					"doctype": doc.doctype,
					"name": doc.name,
					"revision": doc.document_revision,
				},
				user=user,
				after_commit=True,
			)
	# Scheduling failure must not undo review; the scheduler will recover the outbox.
	frappe.db.after_commit.add(_enqueue_dispatch)
	return event


def _enqueue_dispatch() -> None:
	try:
		frappe.enqueue("flutter_utils.verification.notifications.dispatch_due", queue="short")
	except Exception as exc:
		frappe.log_error(title="Verification outbox scheduling failed", message=type(exc).__name__)


def dispatch_due() -> None:
	"""Worker/scheduler entry point. Commit each independent delivery."""
	if not frappe.db.exists("DocType", "Verification Notification Delivery"):
		return
	names = frappe.get_all(
		"Verification Notification Delivery",
		filters={
			"status": ["in", ["Pending", "Failed"]],
			"attempts": ["<", 10],
		},
		or_filters=[["next_attempt_at", "is", "not set"], ["next_attempt_at", "<=", now_datetime()]],
		order_by="creation asc",
		limit_page_length=100,
		pluck="name",
	)
	for name in names:
		deliver(name)
		frappe.db.commit()


def deliver(name: str) -> None:
	frappe.db.get_value("Verification Notification Delivery", name, "name", for_update=True)
	delivery = frappe.get_doc("Verification Notification Delivery", name)
	if delivery.status in {"Delivered", "Skipped"}:
		return
	delivery.attempts = cint(delivery.attempts) + 1
	try:
		event = frappe.get_doc("Verification Review Log", delivery.event)
		doc = frappe.get_doc(event.record_doctype, event.record_name)
		policy = get_policy(doc.source_app)
		allowed = bool(frappe.db.get_value("User", delivery.recipient, "enabled")) and can_access(
			doc, "read", delivery.recipient, policy
		)
	except Exception as exc:
		_failed(delivery, exc)
		with internal_write():
			delivery.save(ignore_permissions=True)
		return
	# Recheck at dispatch: access may have changed since the action.
	if not allowed:
		delivery.status = "Skipped"
		delivery.last_error = "RecipientNoLongerAuthorized"
	else:
		try:
			if delivery.channel == "In App":
				_create_in_app(delivery, event)
			elif delivery.channel == "Push":
				from flutter_utils.push_notifications import send_push_notifications

				push_title, push_body = _push_copy(doc, event)
				sent = send_push_notifications(
					[delivery.recipient],
					push_title,
					push_body,
					app=delivery.push_app,
					# `type` lets the mobile client tell verification pushes apart
					# from call pushes and route the tap to the KYC screen.
					data={
						"type": "verification",
						"action": event.action,
						"event": event.name,
						"record": doc.name,
						"doctype": doc.doctype,
					},
					strict=True,
				)
				if not sent:
					delivery.status = "Skipped"
					delivery.last_error = "PushDisabledOrNoDevices"
			elif delivery.channel == "Email":
				email = frappe.db.get_value("User", delivery.recipient, "email")
				if not email:
					delivery.status = "Skipped"
					delivery.last_error = "EmailUnavailable"
				else:
					message = delivery.message
					if delivery.link:
						url = (
							frappe.utils.get_url(delivery.link)
							if delivery.link.startswith("/")
							else delivery.link
						)
						message += f'<p><a href="{escape(url, quote=True)}">View verification details</a></p>'
					frappe.sendmail(
						recipients=[email],
						subject=delivery.subject,
						message=message,
						reference_doctype=doc.doctype,
						reference_name=doc.name,
						message_id=f"verification-{delivery.name}@{urlsplit(frappe.utils.get_url('/')).hostname or 'localhost'}",
						delayed=True,
					)
			if delivery.status != "Skipped":
				delivery.status = "Delivered"
				delivery.last_error = None
		except Exception as exc:
			_failed(delivery, exc)
	with internal_write():
		delivery.save(ignore_permissions=True)


def _failed(delivery: Any, exc: Exception) -> None:
	delivery.status = "Failed"
	delivery.last_error = type(exc).__name__
	delivery.next_attempt_at = add_to_date(now_datetime(), minutes=min(2**delivery.attempts, 1440))
	frappe.log_error(title="Verification notification delivery failed", message=type(exc).__name__)


def _create_in_app(delivery: Any, event: Any) -> None:
	from flutter_utils.verification.notification_log import verification_notification

	# Deterministic ID plus the locked outbox row prevent duplicate persistent logs.
	name = f"verification-{delivery.name}"
	if not frappe.db.exists("Notification Log", name):
		payload = {
			"doctype": "Notification Log",
			"subject": delivery.subject,
			"email_content": delivery.message,
			"for_user": delivery.recipient,
			"from_user": event.actor,
			"type": "Alert",
			"document_type": event.record_doctype,
			"document_name": event.record_name,
			"link": delivery.link,
		}
		if frappe.get_meta("Notification Log").has_field("app"):
			payload["app"] = event.source_app
		with verification_notification():
			frappe.get_doc(payload).insert(ignore_permissions=True, set_name=name)
	else:
		# Recover a partial first attempt where insertion succeeded but badge/cache
		# update failed, without inserting another persistent notification.
		from frappe.desk.doctype.notification_log.notification_log import set_notifications_as_unseen

		frappe.publish_realtime("notification", after_commit=True, user=delivery.recipient)
		set_notifications_as_unseen(delivery.recipient)
	delivery.notification_log = name
