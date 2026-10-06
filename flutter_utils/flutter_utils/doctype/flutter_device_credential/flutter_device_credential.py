# Copyright (c) 2026, CoreAxis Solutions and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class FlutterDeviceCredential(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Data
		api_secret: DF.Password
		device_id_hash: DF.Data
		device_key: DF.Data
		device_name: DF.Data | None
		enabled: DF.Check
		last_login_at: DF.Datetime
		revocation_reason: (
			DF.Literal["", "Device Logout", "Device Limit", "Limit Reduced", "Credential Rotated"] | None
		)
		revoked_at: DF.Datetime | None
		user: DF.Link
	# end: auto-generated types

	def before_save(self) -> None:
		# Passwords are encrypted later in Document._validate. Capture the previous
		# generation before an administrator replaces the secret through ORM save.
		if not self.is_new() and self.api_secret and self.api_secret != "********":
			from flutter_utils.realtime import disconnect_rotated_device_sockets

			disconnect_rotated_device_sockets(self.name)

	def on_update(self) -> None:
		from flutter_utils.realtime import disconnect_device_sockets

		previous = self.get_doc_before_save()
		if previous and previous.api_key != self.api_key:
			disconnect_device_sockets(previous.api_key)
		if not self.enabled:
			from flutter_utils.push_notifications import deactivate_device_push

			deactivate_device_push(self.name)
			disconnect_device_sockets(self.api_key)

	def on_trash(self) -> None:
		from flutter_utils.realtime import disconnect_device_sockets

		disconnect_device_sockets(self.api_key)
