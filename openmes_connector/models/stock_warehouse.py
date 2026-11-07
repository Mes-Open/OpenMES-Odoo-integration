from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    openmes_code = fields.Char(
        "OpenMES Warehouse Code",
        copy=False,
        help="Code (or ERP code) of the matching warehouse in OpenMES. When set, "
        "on-hand quantities of manufactured products and their components are "
        "pushed to OpenMES as a stock snapshot.",
    )

    @api.constrains("openmes_code", "company_id")
    def _check_openmes_code(self):
        for warehouse in self:
            code = warehouse.openmes_code
            if code and (len(code) > 100 or code != code.strip()):
                raise ValidationError(
                    _("The OpenMES warehouse code must be at most 100 characters, without leading or trailing spaces.")
                )
            # A Python check, not SQL: an upgrade with duplicates must not fail.
            # Two warehouses with one code would book every document nowhere
            # and send the snapshot of that OpenMES warehouse twice.
            if code and self.sudo().with_context(active_test=False).search_count([
                ("id", "!=", warehouse.id),
                ("openmes_code", "=", code),
                ("company_id", "=", warehouse.company_id.id),
            ], limit=1):
                raise ValidationError(
                    _(
                        "Another warehouse of company %(company)s already has the OpenMES warehouse code "
                        "“%(code)s”.",
                        company=warehouse.company_id.name,
                        code=code,
                    )
                )
