from odoo import fields, models


class OpenmesSyncLog(models.Model):
    _name = "openmes.sync.log"
    _description = "OpenMES Synchronisation Log"
    _order = "id desc"
    _rec_name = "operation"

    operation = fields.Selection(
        [
            ("products", "Products"),
            ("routings", "Routings"),
            ("materials", "Materials"),
            ("boms", "Bills of Materials"),
            ("stock", "Stock"),
            ("material_lots", "Material lots"),
            ("stock_documents", "Stock Documents"),
            ("lines", "Production Lines"),
        ],
        required=True,
        readonly=True,
    )
    state = fields.Selection(
        [("success", "Success"), ("partial", "Partial"), ("failed", "Failed")],
        required=True,
        readonly=True,
    )
    sent_count = fields.Integer("Rows Sent", readonly=True)
    imported_count = fields.Integer("Created", readonly=True)
    updated_count = fields.Integer("Updated", readonly=True)
    skipped_count = fields.Integer("Skipped", readonly=True)
    error_count = fields.Integer("Errors", readonly=True)
    message = fields.Text(readonly=True)
    company_id = fields.Many2one(
        "res.company",
        "Company",
        readonly=True,
        index=True,
        default=lambda self: self.env.company,
        help="The company whose OpenMES instance was synchronised.",
    )
