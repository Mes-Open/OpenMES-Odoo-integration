from odoo import api, fields, models

from .openmes_stock_document import MOVE_REFERENCE


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

    @api.depends("openmes_stock_document_id.name")
    def _compute_reference(self):
        # Odoo 19 has no move name any more and gives a move outside a picking
        # no reference: moves booked from an OpenMES document are named after it.
        openmes = self.filtered("openmes_stock_document_id")
        for move in openmes:
            move.reference = MOVE_REFERENCE % move.openmes_stock_document_id.name
        super(StockMove, self - openmes)._compute_reference()

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
