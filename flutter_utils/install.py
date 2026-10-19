import frappe
from frappe import _

from flutter_utils.patches.v1_0.merge_push_registration import execute as merge_push_registration
from flutter_utils.patches.v1_0.remove_user_workspace_sidebar_fields import (
	execute as remove_user_workspace_sidebar_fields,
)


def after_install():
	create_default_otp_template()
	remove_user_workspace_sidebar_fields()
	merge_push_registration()
	from flutter_utils.verification.setup import seed_defaults

	seed_defaults()


def create_default_otp_template():
	if not frappe.db.exists("Email Template", _("OTP Email Template")):
		otp_template = frappe.new_doc("Email Template")
		otp_template.name = _("OTP Email Template")
		otp_template.subject = _("Your OTP for {{ app_name }}")
		otp_template.response = """
<div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px;">
	<h2 style="color:#3525CD;margin-bottom:4px;">{{ app_name }}</h2>
	<p>Use the OTP below to {{ action }}. It expires in <strong>10 minutes</strong>.</p>
	<div style="font-size:40px;font-weight:bold;letter-spacing:10px;color:#3525CD;
	            background:#f0efff;padding:24px;border-radius:12px;
	            text-align:center;margin:24px 0;">
		{{ otp }}
	</div>
	<p style="color:#888;font-size:13px;">
		If you did not request this, you can safely ignore this email.
	</p>
</div>
"""
		otp_template.insert(ignore_permissions=True)
