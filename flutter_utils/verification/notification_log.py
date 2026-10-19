from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import frappe

_verification_notification: ContextVar[bool] = ContextVar("verification_notification", default=False)


@contextmanager
def verification_notification() -> Iterator[None]:
	token = _verification_notification.set(True)
	try:
		yield
	finally:
		_verification_notification.reset(token)


class VerificationNotificationMixin:
	"""Suppress implicit Notification Log email only for our explicit outbox insert.

	Email is a separate policy-controlled delivery. Ordinary notification producers
	continue using the original Frappe behavior.
	"""

	def after_insert(self) -> None:
		if not _verification_notification.get():
			return super().after_insert()
		from frappe.desk.doctype.notification_log.notification_log import set_notifications_as_unseen

		frappe.publish_realtime("notification", after_commit=True, user=self.for_user)
		set_notifications_as_unseen(self.for_user)
