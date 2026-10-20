from typing import Any

from flutter_utils.email import SENDGRID_SERVICE


class SendgridEmailAccountMixin:
	def validate(self) -> None:
		if self.service == SENDGRID_SERVICE:
			self.auth_method = "Basic"
			self.enable_incoming = 0
			self.always_use_account_email_id_as_sender = 1
			self.no_smtp_authentication = 1
		super().validate()

	def validate_smtp_conn(self) -> None:
		if self.service != SENDGRID_SERVICE:
			return super().validate_smtp_conn()

	def get_smtp_server(self) -> Any:
		if self.service != SENDGRID_SERVICE:
			return super().get_smtp_server()
		return None
