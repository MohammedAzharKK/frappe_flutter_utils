from typing import Any

import frappe
from frappe.utils import cint

from flutter_utils.verification.model import publish_updates, validity
from flutter_utils.verification.service import (
	_copy_evidence_revision,
	_emit,
	_persist,
	lock_record,
	reevaluate_request,
)


def refresh_validity() -> None:
	"""Page through evidence and commit independent expiry updates; no automatic purge."""
	if not frappe.db.exists("DocType", "Verification Document"):
		return
	configs = frappe.get_all("Verification Document Type", fields=["name", "warning_days"])
	warnings = {row.name: cint(row.warning_days) or 30 for row in configs}
	cursor = ""
	while True:
		rows = frappe.get_all(
			"Verification Document",
			filters={
				"name": [">", cursor],
				"expiry_date": ["is", "set"],
				"is_archived": 0,
				"is_withdrawn": 0,
			},
			fields=["name", "document_type", "expiry_date", "validity_status"],
			order_by="name asc",
			limit_page_length=100,
		)
		if not rows:
			break
		for row in rows:
			cursor = row.name
			if validity(row.expiry_date, warnings.get(row.document_type, 30)) != row.validity_status:
				try:
					_refresh_one(row.name, warnings)
					frappe.db.commit()
				except Exception as exc:
					frappe.db.rollback()
					frappe.log_error(title="Verification validity refresh failed", message=type(exc).__name__)


def _refresh_one(name: str, warnings: dict[str, int]) -> None:
	doc = lock_record("Verification Document", name)
	if doc.is_archived or doc.is_withdrawn:
		return
	status = validity(doc.expiry_date, warnings.get(doc.document_type, 30))
	if status == doc.validity_status:
		return
	doc.validity_status = status
	doc.document_revision += 1
	_copy_evidence_revision(doc, doc.document_revision - 1)
	_persist(doc)
	if status in {"Expiring Soon", "Expired"}:
		_emit(doc, status, doc.approval_status)
	reevaluate_request(doc.verification_request)
	frappe.db.after_commit.add(lambda: _publish(doc))


def _publish(doc: Any) -> None:
	# Defensive post-commit worker emission; normal ORM notification is retained too.
	publish_updates(doc, after_commit=False)
