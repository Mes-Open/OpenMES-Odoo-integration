from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ProductTemplate(models.Model):
    _inherit = "product.template"

    openmes_manufacture = fields.Boolean(
        "Manufacture in OpenMES",
        help="Confirming a sale order or a manufacturing order (see Settings → "
        "OpenMES) with this product creates a work order in OpenMES. The "
        "product's Internal Reference is used as the OpenMES product type code.",
    )
    openmes_line_id = fields.Many2one(
        "openmes.line",
        "OpenMES Line",
        ondelete="restrict",
        domain="[('company_id', 'in', [company_id] if company_id else allowed_company_ids)]",
        help="OpenMES production line for this product. Leave empty to use the "
        "default line from Settings. The list is refreshed from OpenMES. A product "
        "shared by several companies uses this line only in the line's company; "
        "the other companies use their default line.",
    )
    # Kept for compatibility (RPC, imports): reads and writes go through openmes_line_id.
    openmes_line_code = fields.Char(
        "OpenMES Line Code",
        compute="_compute_openmes_line_code",
        inverse="_inverse_openmes_line_code",
        search="_search_openmes_line_code",
        help="Code of the OpenMES production line for this product. Leave empty "
        "to use the default line from Settings.",
    )

    @api.constrains("openmes_line_id", "company_id")
    def _check_openmes_line_company(self):
        for template in self.sudo():
            line = template.openmes_line_id
            if line and template.company_id and line.company_id != template.company_id:
                raise ValidationError(
                    _(
                        "Product %(product)s belongs to %(company)s, but OpenMES line %(line)s belongs to "
                        "another company.",
                        product=template.display_name,
                        company=template.company_id.name,
                        line=line.code,
                    )
                )

    @api.depends("openmes_line_id.code")
    def _compute_openmes_line_code(self):
        for template in self:
            template.openmes_line_code = template.openmes_line_id.code or False

    def _openmes_line_code_for(self, company):
        """Line code of a work order of ``company``: the product's line when it
        belongs to that company, otherwise the company's default line (a
        shared product can only name one company's line)."""
        self.ensure_one()
        line = self.sudo().openmes_line_id
        if line and line.company_id == company:
            return line.code
        return company.sudo().openmes_default_line_id.code or False

    def _inverse_openmes_line_code(self):
        Line = self.env["openmes.line"].sudo().with_context(active_test=False)
        for template in self:
            code = template.openmes_line_code
            if not code:
                template.openmes_line_id = False
                continue
            company = template.company_id or self.env.company
            line = Line.search([("code", "=", code), ("company_id", "=", company.id)], limit=1)
            if not line:
                raise ValidationError(
                    _("Unknown OpenMES line “%s”. Refresh the line list in Settings → OpenMES.", code)
                )
            template.openmes_line_id = line

    def _search_openmes_line_code(self, operator, value):
        return [("openmes_line_id.code", operator, value)]
