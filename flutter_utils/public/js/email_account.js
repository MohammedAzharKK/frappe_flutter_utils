// Keep the service value compatible with existing frappe_sendgrid accounts.
frappe.email_defaults["Sendgrid HTTP"] = {
	auth_method: "Basic",
	domain: null,
	email_server: null,
	incoming_port: 0,
	use_ssl: 0,
	use_imap: 0,
	use_starttls: 0,
	validate_ssl_certificate: 0,
	enable_incoming: 0,
	enable_outgoing: 1,
	smtp_server: null,
	smtp_port: null,
	use_tls: 0,
	use_ssl_for_outgoing: 0,
	no_smtp_authentication: 1,
	always_use_account_email_id_as_sender: 1,
	login_id_is_different: 0,
	login_id: null,
};

function set_sendgrid_key_description(frm) {
	if (frm.doc.service !== "Sendgrid HTTP") return;
	frm.set_df_property(
		"password",
		"description",
		__("Enter your SendGrid API Key here (starts with SG.)")
	);
}

frappe.ui.form.on("Email Account", {
	refresh: set_sendgrid_key_description,
	service: set_sendgrid_key_description,
});
