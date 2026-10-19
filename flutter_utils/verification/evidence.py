from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePath
from types import SimpleNamespace
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from frappe.utils.verified_command import get_signed_params

from flutter_utils.verification.media import (
	SUPPORTED_EXTENSIONS,
	VIDEO_EXTENSIONS,
	matches_signature,
	validate_mp4,
)


def get_max_file_size() -> int:
	from frappe.core.api.file import get_max_file_size as frappe_max_file_size

	return frappe_max_file_size()


def _fail(label: str | None, message: str) -> None:
	"""Raise a validation error prefixed with the failing document, when known."""
	frappe.throw(f"{label}: {message}" if label else message)


def check_content(filename: str, content: bytes, config: Any, label: str | None = None) -> None:
	extension = PurePath(filename).suffix.lower().lstrip(".")
	display = PurePath(filename or "").name or (extension.upper() or "file")
	allowed = {value.strip().lower().lstrip(".") for value in (config.allowed_extensions or "").splitlines()}
	if extension not in allowed or extension not in SUPPORTED_EXTENSIONS:
		_fail(label, _("'{0}' is not an accepted file format.").format(display))
	if not content or len(content) > min(cint(config.max_file_size_mb) * 1024 * 1024, get_max_file_size()):
		_fail(label, _("'{0}' is empty or exceeds the upload limit.").format(display))
	if extension in VIDEO_EXTENSIONS:
		validate_mp4(content, label or "")
		return
	# Do not trust the filename or client MIME type. Only inert document/image formats.
	if not matches_signature(extension, content):
		_fail(
			label,
			_(
				"'{0}' does not look like a real {1} file. It may be HEIC or mislabeled; choose another file."
			).format(display, extension.upper()),
		)


@contextmanager
def owned_private_upload(file_id: str) -> Iterator[Any]:
	"""Import an applicant's fresh, unattached local upload without reparenting it.

	Server-only adapter for pre-upload APIs. The normal evidence service validates,
	creates a new private File, and records the revision. Never reads an arbitrary URL.
	"""
	file = frappe.get_doc("File", file_id, for_update=True)
	if (
		frappe.session.user == "Guest"
		or file.owner != frappe.session.user
		or not file.is_private
		or file.attached_to_name
	):
		frappe.throw(_("Use your own unattached private upload."), frappe.PermissionError)
	if not (file.file_url or "").startswith("/private/files/"):
		frappe.throw(_("Only local private uploads can be imported."), frappe.PermissionError)
	file.validate_file_path()
	with open(file.get_full_path(), "rb") as stream:
		yield SimpleNamespace(filename=file.file_name, stream=stream)


def add_evidence(doc: Any, uploads: list[tuple[Any, str]]) -> None:
	config = frappe.get_doc("Verification Document Type", doc.document_type)
	if not 1 <= len(uploads) <= cint(config.max_files):
		frappe.throw(_("Evidence file count exceeds the document type limit."))
	roles = {role.strip() for role in (config.allowed_roles or "").splitlines()}
	for upload, role in uploads:
		if role not in roles:
			frappe.throw(_("Invalid evidence role."))
		# Bounded stream read: never load an arbitrarily large upload into memory.
		limit = min(cint(config.max_file_size_mb) * 1024 * 1024, get_max_file_size())
		content = upload.stream.read(limit + 1)
		filename = PurePath(upload.filename or "").name
		check_content(filename, content, config, doc.document_type)
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": filename,
				"content": content,
				"is_private": 1,
				"attached_to_doctype": doc.doctype,
				"attached_to_name": doc.name,
				"attached_to_field": "files",
			}
		).insert(ignore_permissions=True)
		doc.append(
			"files",
			{
				"file": file.name,
				"evidence_role": role,
				"revision": doc.document_revision,
				"uploaded_by": frappe.session.user,
				"uploaded_at": now_datetime(),
				"content_hash": hashlib.sha256(content).hexdigest(),
			},
		)
		if upload is uploads[0][0]:
			doc.primary_file = file.name


def evidence_urls(files: list[Any]) -> dict[str, str]:
	"""Shared signing pattern, using file IDs and a permission-checking download route.

	The installed Frappe download routes vary by version. Do not expose stored paths
	or depend on a legacy image endpoint; retain authentication on the new route.
	"""
	endpoint = frappe.utils.get_url("/api/method/flutter_utils.api.verification.download_evidence")
	result = {}
	for file in files:
		if file.is_private and file.attached_to_doctype == "Verification Document":
			try:
				result[file.name] = f"{endpoint}?{get_signed_params({'file_id': file.name})}"
			except Exception as exc:
				frappe.log_error(title="Verification evidence signing failed", message=type(exc).__name__)
	return result


def current_evidence(doc: Any) -> list[Any]:
	return [row for row in doc.files if cint(row.revision) == cint(doc.document_revision)]


def current_rows(docs: list[Any]) -> list[Any]:
	"""Fetch only the exact current revisions, not every historical child row."""
	if not docs:
		return []
	table = frappe.qb.DocType("Verification Document File")
	condition = (table.parent == docs[0].name) & (table.revision == docs[0].document_revision)
	for doc in docs[1:]:
		condition |= (table.parent == doc.name) & (table.revision == doc.document_revision)
	return (
		frappe.qb.from_(table)
		.select(table.star)
		.where(condition & (table.parenttype == "Verification Document") & (table.parentfield == "files"))
		.orderby(table.idx)
		.run(as_dict=True)
	)
