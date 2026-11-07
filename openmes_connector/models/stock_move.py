from odoo import fields, models


class StockMove(models.Model):
    _inherit = "stock.move"

    openmes_stock_document_id = fields.Many2one(
        "openmes.stock.document",
        "OpenMES Stock Document",
        index="btree_not_null",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    def write(self, vals):
        # An MO's deadline is computed from its finished moves, so a new
        # commitment date on the sale order reaches the MO through them and
        # never through mrp.production.write.
        if "date_deadline" not in vals:
            return super().write(vals)
        productions = self.sudo().production_id
        before = productions._openmes_snapshots()
        res = super().write(vals)
        productions._openmes_resend_changed(before)
        return res
