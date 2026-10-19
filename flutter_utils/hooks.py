app_name = "flutter_utils"
app_title = "Flutter Utils"
app_publisher = "CoreAxis Solutions"
app_description = "Flutter utility APIs for Frappe - exception handling and email OTP authentication"
app_email = "lubshad4u4@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Patch Frappe's exception handler with the Flutter-friendly one.
# Uses before_request since on_app_init is not a real Frappe hook.
before_request = ["flutter_utils.utils.patch_exception_handler"]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/flutter_utils/css/flutter_utils.css"
# app_include_js = "/assets/flutter_utils/js/flutter_utils.js"

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Installation
# ------------

# before_install = "flutter_utils.install.before_install"
after_install = "flutter_utils.install.after_install"
after_migrate = "flutter_utils.verification.setup.seed_defaults"

# Uninstallation
# ------------

# before_uninstall = "flutter_utils.uninstall.before_uninstall"
# after_uninstall = "flutter_utils.uninstall.after_uninstall"

# Document Events
# ---------------

doc_events = {
	"User": {"on_update": "flutter_utils.realtime.disconnect_disabled_user_sockets"},
	"File": {
		"before_validate": "flutter_utils.verification.permissions.protect_file",
		"on_trash": "flutter_utils.verification.permissions.protect_file",
	},
}

# Verification is policy-driven. Other apps register dotted VerificationPolicy
# subclass paths in their own verification_policies hook dictionaries.
has_permission = {
	"Verification Document": "flutter_utils.verification.permissions.has_permission",
	"Verification Request": "flutter_utils.verification.permissions.has_permission",
	"Verification Review Log": "flutter_utils.verification.permissions.has_permission",
	"Verification Notification Delivery": "flutter_utils.verification.permissions.has_permission",
	"File": "flutter_utils.verification.permissions.file_permission",
}
permission_query_conditions = {
	"File": "flutter_utils.verification.permissions.file_query_conditions",
	"Verification Document": "flutter_utils.verification.permissions.document_query_conditions",
	"Verification Request": "flutter_utils.verification.permissions.request_query_conditions",
	"Verification Review Log": "flutter_utils.verification.permissions.deny_query",
	"Verification Notification Delivery": "flutter_utils.verification.permissions.deny_query",
}
extend_doctype_class = {
	"Notification Log": ["flutter_utils.verification.notification_log.VerificationNotificationMixin"],
	"File": ["flutter_utils.verification.file.VerificationFileMixin"],
}
doctype_js = {
	"Verification Document": "public/js/verification.js",
	"Verification Request": "public/js/verification.js",
}
scheduler_events = {
	"daily": ["flutter_utils.verification.tasks.refresh_validity"],
	"cron": {"*/5 * * * *": ["flutter_utils.verification.notifications.dispatch_due"]},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"daily": [
# 		"flutter_utils.tasks.daily"
# 	],
# }

# Overriding Methods
# ------------------

# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "flutter_utils.event.get_events"
# }

# Authentication and authorization
# ---------------------------------

auth_hooks = ["flutter_utils.auth.validate"]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True
