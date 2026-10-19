/* Desk is a review surface; mutations use the same service as external clients. */
for (const doctype of ["Verification Document", "Verification Request"]) {
	frappe.ui.form.on(doctype, {
		async refresh(frm) {
			frm.disable_save();
			if (frm.is_new()) {
				frm.dashboard.set_headline(__("Create submissions through the verification API."));
				return;
			}
			const request = doctype === "Verification Request";
			const { message: record } = await frappe.call({
				method: `flutter_utils.api.verification.${request ? "get_request" : "get_document"}`,
				args: { [request ? "request_id" : "document_id"]: frm.doc.name },
			});
			if (frm.doc.name !== record.id) return;
			for (const action of record.allowed_actions) {
				if (!["approve", "reject", "revoke", "reset", "resubmit", "withdraw", "archive", "assign"].includes(action)) continue;
				frm.add_custom_button(__(action.charAt(0).toUpperCase() + action.slice(1)), () => {
					if (action === "assign") {
						frappe.prompt([{ fieldname: "reviewer", label: __("Reviewer"), fieldtype: "Link", options: "User", reqd: 1 }], async (values) => {
							await frappe.call({ method: "flutter_utils.api.verification.assign_reviewer", args: {
								record_doctype: doctype, record_id: record.id, reviewer: values.reviewer,
								expected_revision: record.document_revision,
							}, freeze: true });
							await frm.reload_doc();
						});
						return;
					}
					const execute = async (reason) => {
						const decisions = { approve: "Approved", reject: "Rejected", revoke: "Revoked", reset: "Reset", resubmit: "Resubmitted" };
						const args = { [request ? "request_id" : "document_id"]: record.id, expected_revision: record.document_revision };
						let method = `${action}_${request ? "request" : "document"}`;
						if (request && decisions[action]) {
							method = "review_request";
							args.action = decisions[action];
						}
						if (reason) args.reason = reason;
						await frappe.call({ method: `flutter_utils.api.verification.${method}`, args, freeze: true });
						await frm.reload_doc();
					};
					if (["reject", "revoke"].includes(action)) {
						frappe.prompt([{ fieldname: "reason", label: __("Reason"), fieldtype: "Small Text", reqd: 1 }], (values) => execute(values.reason));
					} else {
						frappe.confirm(__("{0} verification record “{1}”?", [
							__(action.charAt(0).toUpperCase() + action.slice(1)),
							frappe.utils.escape_html(record.title || record.id),
						]), () => execute());
					}
				}, __("Verification"));
			}
			frm.add_custom_button(__("Review History"), async () => {
				const { message: history } = await frappe.call({ method: "flutter_utils.api.verification.get_review_history", args: {
					record_doctype: doctype, record_id: record.id, page_size: 50,
				} });
				frappe.msgprint({ title: __("Review History"), message: history.map((event) =>
					`<p>${frappe.utils.escape_html(event.action)} — ${frappe.utils.escape_html(String(event.occurred_at))}<br>${frappe.utils.escape_html(event.reason || "")}</p>`
				).join("") || __("No review history.") });
			});
		},
	});
}
