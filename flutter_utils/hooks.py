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

# Uninstallation
# ------------

# before_uninstall = "flutter_utils.uninstall.before_uninstall"
# after_uninstall = "flutter_utils.uninstall.after_uninstall"

# Document Events
# ---------------

doc_events = {
	"User": {"on_update": "flutter_utils.realtime.disconnect_disabled_user_sockets"},
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
