from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# Limit of steps.*.workstation_code in /process-templates/import.
MAX_WORKSTATION_CODE = 50


class MrpWorkcenter(models.Model):
    _inherit = "mrp.workcenter"

    openmes_workstation_code = fields.Char(
        "OpenMES Workstation Code",
        copy=False,
        index=True,
        help="Code of the matching OpenMES workstation. When set, BoM operations "
        "on this work center are sent to OpenMES with this workstation (and the "
        "product's OpenMES line) at the next master data sync.",
    )

    @api.constrains("openmes_workstation_code", "company_id")
    def _check_openmes_workstation_code(self):
        for workcenter in self:
            code = workcenter.openmes_workstation_code
            if not code:
                continue
            if len(code) > MAX_WORKSTATION_CODE or code != code.strip():
                raise ValidationError(
                    _(
                        "The OpenMES workstation code must be at most 50 characters, "
                        "without leading or trailing spaces."
                    )
                )
            # A Python check, not SQL: an upgrade with duplicates must not fail.
            # A work center without a company is used by every company, so it
            # clashes with the same code anywhere.
            domain = [("id", "!=", workcenter.id), ("openmes_workstation_code", "=", code)]
            if workcenter.company_id:
                domain.append(("company_id", "in", [False, workcenter.company_id.id]))
            clash = self.sudo().with_context(active_test=False).search(domain, limit=1)
            if clash:
                raise ValidationError(
                    _(
                        "Work center %(other)s (company %(company)s) already has the OpenMES "
                        "workstation code “%(code)s”.",
                        other=clash.display_name,
                        company=clash.company_id.name or _("all companies"),
                        code=code,
                    )
                )
