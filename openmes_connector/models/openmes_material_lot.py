"""Lots of materials the OpenMES instance of a company holds a balance for.

``material-lots/import`` sets the balance of the lots it names and leaves the
others alone, so a lot that ran out in Odoo has to be sent once more with 0.
Odoo forgets an empty lot (its quants are deleted), so every lot sent with a
quantity is remembered here per warehouse until OpenMES accepted its 0.
"""

from odoo import fields, models


class OpenmesMaterialLotSent(models.Model):
    _name = "openmes.material.lot.sent"
    _description = "OpenMES Material Lot Sent"
    _order = "id"

    company_id = fields.Many2one("res.company", "Company", required=True, readonly=True, index=True)
    warehouse_id = fields.Many2one(
        "stock.warehouse", "Warehouse", required=True, readonly=True, index=True, ondelete="cascade"
    )
    # Kept as text: the lot and the product may be renamed or deleted in Odoo,
    # the 0 must still name what OpenMES knows.
    material_code = fields.Char("Material Code", required=True, readonly=True)
    lot_number = fields.Char("Lot Number", required=True, readonly=True)
    unit_of_measure = fields.Char("Unit", readonly=True)
    product_id = fields.Many2one("product.product", "Product", readonly=True, ondelete="set null")
    lot_id = fields.Many2one("stock.lot", "Lot", readonly=True, ondelete="set null")
    zero_refusals = fields.Integer(
        "Refused Zeros", readonly=True, help="Runs in a row in which OpenMES refused this lot's 0."
    )

    _sql_constraints = [
        (
            "warehouse_lot_unique",
            "unique(warehouse_id, material_code, lot_number)",
            "A lot is remembered once per warehouse.",
        ),
    ]
