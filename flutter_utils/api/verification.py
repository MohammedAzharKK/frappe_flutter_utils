from __future__ import annotations

import hmac
import mimetypes
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.verified_command import _sign_message
from werkzeug.utils import send_file

from flutter_utils.verification import service
from flutter_utils.verification.evidence import current_rows, evidence_urls, get_max_file_size
from flutter_utils.verification.policies import require


def _authenticated() -> None:
	if frappe.session.user == "Guest":
		raise frappe.AuthenticationError


def _data(data: dict[str, Any] | str) -> dict[str, Any]:
	value = frappe.parse_json(data) if isinstance(data, str) else data
	if not isinstance(value, dict):
		frappe.throw(_("Verification data must be an object."))
	return value


def _uploads(evidence_roles: list[str] | str | None) -> list[tuple[Any, str]]:
	files = list(frappe.request.files.getlist("files")) if frappe.request else []
	if not files:
		return []
	roles = frappe.parse_json(evidence_roles) if isinstance(evidence_roles, str) else evidence_roles
	if (
		not isinstance(roles, list)
		or len(roles) != len(files)
		or not all(isinstance(role, str) for role in roles)
	):
		frappe.throw(_("Provide one evidence role for each uploaded file."))
	return list(zip(files, roles, strict=True))


def _get(doctype: str, name: str) -> Any:
	_authenticated()
	row = frappe.db.get_value(doctype, name, "*", as_dict=True)
	if not row:
		frappe.throw(_("Verification record not found."), frappe.DoesNotExistError)
	doc = frappe.get_doc({"doctype": doctype, **row})
	require(doc, "read")
	if doctype == "Verification Document":
		doc.set("files", current_rows([doc]))
	return doc


@frappe.whitelist()
def get_document(document_id: str) -> dict[str, Any]:
	return service.serialize(_get("Verification Document", document_id))


@frappe.whitelist()
def get_request(request_id: str) -> dict[str, Any]:
	return service.serialize(_get("Verification Request", request_id))


def _list(
	doctype: str,
	source_app: str | None,
	reference_doctype: str | None,
	reference_name: str | None,
	offset: int,
	page_size: int,
	verification_purpose: str | None = None,
) -> dict[str, Any]:
	_authenticated()
	page_size = min(max(cint(page_size), 1), 50)
	offset = max(cint(offset), 0)
	source_app = source_app or "flutter_utils"
	policy = service.get_policy(source_app)
	if reference_name and not reference_doctype:
		# Built-in convenience; custom references always require the pair.
		if source_app != "flutter_utils":
			frappe.throw(_("Provide both subject DocType and name."))
		reference_doctype = "User"
	if reference_doctype and reference_name:
		purpose = verification_purpose or (policy.purposes[0] if policy.purposes else None)
		context = frappe._dict(
			source_app=source_app,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			verification_purpose=purpose,
		)
		require(context, "read")
		filters = {"reference_doctype": reference_doctype, "reference_name": reference_name}
		if source_app != "flutter_utils":
			filters["verification_purpose"] = purpose
	else:
		filters = policy.list_filters(frappe.session.user)
		if filters is None:
			frappe.throw(
				_("This integration requires an explicit subject for listing."), frappe.PermissionError
			)
		filters = dict(filters)
		if reference_doctype:
			if filters.get("reference_doctype") not in (None, reference_doctype):
				frappe.throw(_("Subject is outside the permitted listing scope."), frappe.PermissionError)
			filters["reference_doctype"] = reference_doctype
	filters["source_app"] = source_app
	if verification_purpose:
		if filters.get("verification_purpose") not in (None, verification_purpose):
			frappe.throw(_("Purpose is outside the permitted listing scope."), frappe.PermissionError)
		filters["verification_purpose"] = verification_purpose
	# Scope before pagination; do not scan unrelated subjects or expose their counts.
	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields="*",
		order_by="creation desc, name desc",
		limit_start=offset,
		limit_page_length=page_size,
	)
	docs = []
	for row in rows:
		policy = service.get_policy(row.source_app)
		if policy.can(row, "read", frappe.session.user):
			docs.append(frappe.get_doc({"doctype": doctype, **row}))
	if doctype == "Verification Document" and docs:
		children = current_rows(docs)
		grouped: dict[str, list[Any]] = {}
		for row in children:
			grouped.setdefault(row.parent, []).append(row)
		for doc in docs:
			doc.set("files", grouped.get(doc.name, []))
		ids = {row.file for doc in docs for row in doc.files if row.revision == doc.document_revision}
		files = (
			frappe.get_all(
				"File",
				filters={"name": ["in", list(ids)]},
				fields=["name", "file_name", "is_private", "attached_to_doctype", "attached_to_name"],
			)
			if ids
			else []
		)
		file_map = {file.name: file for file in files}
	else:
		file_map = {}
	counts = (
		service.usable_document_counts([doc.name for doc in docs])
		if doctype == "Verification Request"
		else {}
	)
	return {
		"items": [service.serialize(doc, file_map, counts.get(doc.name, {})) for doc in docs],
		"next_offset": offset + len(rows) if len(rows) == page_size else None,
	}


@frappe.whitelist()
def get_documents(
	source_app: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	offset: int = 0,
	page_size: int = 20,
	verification_purpose: str | None = None,
) -> dict[str, Any]:
	return _list(
		"Verification Document",
		source_app,
		reference_doctype,
		reference_name,
		offset,
		page_size,
		verification_purpose,
	)


@frappe.whitelist()
def get_requests(
	source_app: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	offset: int = 0,
	page_size: int = 20,
	verification_purpose: str | None = None,
) -> dict[str, Any]:
	return _list(
		"Verification Request",
		source_app,
		reference_doctype,
		reference_name,
		offset,
		page_size,
		verification_purpose,
	)


@frappe.whitelist()
def get_document_types(
	source_app: str, reference_doctype: str, reference_name: str, verification_purpose: str
) -> list[dict[str, Any]]:
	_authenticated()
	context = frappe._dict(
		source_app=source_app,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		verification_purpose=verification_purpose,
	)
	policy = require(context, "create")
	return frappe.get_all(
		"Verification Document Type",
		filters={"name": ["in", policy.document_types(context)], "enabled": 1},
		fields=[
			"name",
			"title",
			"required_fields",
			"requires_expiry",
			"allowed_extensions",
			"allowed_roles",
			"max_files",
			"max_file_size_mb",
			"warning_days",
		],
	)


@frappe.whitelist()
def get_upload_config() -> dict[str, int]:
	_authenticated()
	return {"max_file_size_bytes": get_max_file_size()}


@frappe.whitelist(methods=["POST"])
def save_document(
	data: dict[str, Any] | str,
	document_id: str | None = None,
	expected_revision: int | None = None,
	idempotency_key: str | None = None,
	evidence_roles: list[str] | str | None = None,
) -> dict[str, Any]:
	_authenticated()
	return service.serialize(
		service.save_record(
			"Verification Document",
			_data(data),
			document_id,
			expected_revision,
			idempotency_key,
			_uploads(evidence_roles),
		)
	)


@frappe.whitelist(methods=["POST"])
def save_request(
	data: dict[str, Any] | str,
	request_id: str | None = None,
	expected_revision: int | None = None,
	idempotency_key: str | None = None,
) -> dict[str, Any]:
	_authenticated()
	return service.serialize(
		service.save_record(
			"Verification Request", _data(data), request_id, expected_revision, idempotency_key
		)
	)


def _transition(
	doctype: str,
	name: str,
	action: str,
	expected_revision: int,
	reason: str | None = None,
	review_notes: str | None = None,
) -> dict[str, Any]:
	_authenticated()
	return service.serialize(
		service.transition(doctype, name, action, expected_revision, reason, review_notes)
	)


@frappe.whitelist(methods=["POST"])
def approve_document(
	document_id: str, expected_revision: int, review_notes: str | None = None
) -> dict[str, Any]:
	return _transition(
		"Verification Document", document_id, "Approved", expected_revision, review_notes=review_notes
	)


@frappe.whitelist(methods=["POST"])
def reject_document(document_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
	return _transition("Verification Document", document_id, "Rejected", expected_revision, reason)


@frappe.whitelist(methods=["POST"])
def revoke_document(document_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
	return _transition("Verification Document", document_id, "Revoked", expected_revision, reason)


@frappe.whitelist(methods=["POST"])
def reset_document(document_id: str, expected_revision: int) -> dict[str, Any]:
	return _transition("Verification Document", document_id, "Reset", expected_revision)


@frappe.whitelist(methods=["POST"])
def resubmit_document(document_id: str, expected_revision: int) -> dict[str, Any]:
	return _transition("Verification Document", document_id, "Resubmitted", expected_revision)


@frappe.whitelist(methods=["POST"])
def withdraw_document(document_id: str, expected_revision: int) -> dict[str, Any]:
	_authenticated()
	return service.serialize(
		service.deactivate("Verification Document", document_id, "Withdrawn", expected_revision)
	)


@frappe.whitelist(methods=["POST"])
def archive_document(document_id: str, expected_revision: int) -> dict[str, Any]:
	_authenticated()
	return service.serialize(
		service.deactivate("Verification Document", document_id, "Archived", expected_revision)
	)


@frappe.whitelist(methods=["POST"])
def review_request(
	request_id: str,
	action: str,
	expected_revision: int,
	reason: str | None = None,
	review_notes: str | None = None,
) -> dict[str, Any]:
	# Same state machine as documents; explicit allowlist prevents undocumented operations.
	return _transition("Verification Request", request_id, action, expected_revision, reason, review_notes)


@frappe.whitelist(methods=["POST"])
def withdraw_request(request_id: str, expected_revision: int) -> dict[str, Any]:
	_authenticated()
	return service.serialize(
		service.deactivate("Verification Request", request_id, "Withdrawn", expected_revision)
	)


@frappe.whitelist(methods=["POST"])
def archive_request(request_id: str, expected_revision: int) -> dict[str, Any]:
	_authenticated()
	return service.serialize(
		service.deactivate("Verification Request", request_id, "Archived", expected_revision)
	)


@frappe.whitelist(methods=["POST"])
def assign_reviewer(
	record_doctype: str, record_id: str, reviewer: str, expected_revision: int
) -> dict[str, Any]:
	_authenticated()
	return service.serialize(service.assign_reviewer(record_doctype, record_id, reviewer, expected_revision))


@frappe.whitelist()
def get_review_history(
	record_doctype: str, record_id: str, offset: int = 0, page_size: int = 20
) -> list[dict[str, Any]]:
	service._doctype(record_doctype)
	doc = _get(record_doctype, record_id)
	internal = service.get_policy(doc.source_app).can(doc, "history_internal", frappe.session.user)
	fields = ["name", "action", "revision", "status_before", "status_after", "occurred_at", "reason"]
	if internal:
		fields.extend(["actor", "review_notes"])
	return frappe.get_all(
		"Verification Review Log",
		filters={"record_doctype": record_doctype, "record_name": record_id},
		fields=fields,
		order_by="creation desc, name desc",
		limit_start=max(cint(offset), 0),
		limit_page_length=min(max(cint(page_size), 1), 50),
	)


@frappe.whitelist()
def get_evidence_history(document_id: str, offset: int = 0, page_size: int = 20) -> list[dict[str, Any]]:
	doc = _get("Verification Document", document_id)
	rows = frappe.get_all(
		"Verification Document File",
		filters={
			"parent": doc.name,
			"parenttype": "Verification Document",
			"parentfield": "files",
		},
		fields=["file", "evidence_role", "revision"],
		order_by="revision desc, idx asc",
		limit_start=max(cint(offset), 0),
		limit_page_length=min(max(cint(page_size), 1), 50),
	)
	files = (
		frappe.get_all(
			"File",
			filters={"name": ["in", [row.file for row in rows]], "attached_to_name": doc.name},
			fields=["name", "file_name", "is_private", "attached_to_doctype", "attached_to_name"],
		)
		if rows
		else []
	)
	urls = evidence_urls(files)
	return [
		{"id": row.file, "role": row.evidence_role, "revision": row.revision, "url": urls.get(row.file)}
		for row in rows
	]


def _has_valid_signature() -> bool:
	"""True when the request carries a valid signed-params signature.

	Mirrors ``verify_request`` but never renders a response page, so an unsigned
	request can still fall back to credential/session authentication (the admin
	console).
	"""
	request = frappe.request
	if not request or request.method != "GET" or request.form or request.data:
		return False
	flags = getattr(frappe.local, "flags", None)
	signed_query = getattr(flags, "signed_query_string", None) if flags else None
	query = frappe.safe_decode(signed_query or getattr(request, "query_string", None) or "")
	marker = "&_signature="
	if marker not in query:
		return False
	params, given = query.split(marker, 1)
	return hmac.compare_digest(given, _sign_message(params))


@frappe.whitelist(allow_guest=True)
def download_evidence(file_id: str) -> Any:
	"""Serve one evidence file from a signed link or an authenticated session.

	The app (``Image.network``, the video player) and the phone browser fetch a
	signed URL without a session cookie, so a valid HMAC signature (minted only
	for readers who already passed the policy check in ``serialize``) authorizes
	them. The admin console fetches the same method with its device credentials,
	so a logged-in owner/reviewer is authorized by the subject read policy.

	Served inline with the real mimetype so images, videos and PDFs preview.
	Local files stream through Werkzeug for both full and Range responses, so
	large videos are never read into memory and media players can seek.
	"""
	signed = _has_valid_signature()
	if not signed:
		_authenticated()
	file = frappe.get_doc("File", file_id)
	if not file.is_private or file.attached_to_doctype != "Verification Document":
		frappe.throw(_("Not verification evidence."), frappe.PermissionError)
	if not signed:
		# Credential/session callers must still pass the subject read policy.
		_get("Verification Document", file.attached_to_name)
	if not frappe.db.exists(
		"Verification Document File",
		{
			"parent": file.attached_to_name,
			"parenttype": "Verification Document",
			"parentfield": "files",
			"file": file.name,
		},
	):
		frappe.throw(_("Evidence is not linked to this document."), frappe.PermissionError)
	# Browsers and native video players request byte ranges for media. Streaming
	# every local response through Werkzeug keeps large videos out of memory,
	# advertises range support on the first 200, and lets the same handler answer
	# a later request with 206. Remote-storage files keep the legacy path.
	if frappe.request and not str(file.get("file_url") or "").startswith(("http://", "https://")):
		response = send_file(
			file.get_full_path(),
			frappe.request.environ,
			mimetype=mimetypes.guess_type(file.file_name)[0] or "application/octet-stream",
			as_attachment=False,
			download_name=file.file_name,
			conditional=True,
		)
		response.headers.setdefault("Accept-Ranges", "bytes")
		# send_file already sets no-cache, which still permits storing evidence.
		response.headers["Cache-Control"] = "private, no-store"
		response.headers.setdefault("X-Content-Type-Options", "nosniff")
		return response
	frappe.local.response.filename = file.file_name
	frappe.local.response.filecontent = file.get_content()
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"
