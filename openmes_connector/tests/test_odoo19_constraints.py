"""Odoo 19 port: the unique constraints moved from ``_sql_constraints`` to
``models.Constraint``. They must keep their Odoo 18 database names (an upgraded
database already has them) and their messages."""

import psycopg2

from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import OpenmesCase

# model, constraint name without the table prefix, message
CONSTRAINTS = [
    ("openmes.line", "code_uniq", "An OpenMES line with this code already exists in this company."),
    ("openmes.work.order", "name_unique", "An OpenMES order number must be unique."),
    ("openmes.webhook.event", "delivery_id_unique", "An OpenMES webhook delivery is processed only once."),
    ("openmes.material.lot.sent", "warehouse_lot_unique", "A lot is remembered once per warehouse."),
    (
        "openmes.stock.document",
        "mes_id_unique",
        "An OpenMES stock document can only be imported once per company.",
    ),
]


@tagged("post_install", "-at_install")
class TestOdoo19Constraints(OpenmesCase):
    def _records(self):
        """One record per constrained model, plus the values a duplicate copies."""
        company = self.env.company
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
        order = self._create_order([(self.bread, 1, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        self.assertEqual(len(work_order), 1)
        return {
            "openmes.line": (
                self.env["openmes.line"].create({"code": "L9", "name": "Line nine"}),
                {"code": "L9"},
            ),
            "openmes.work.order": (work_order, {"name": work_order.name}),
            "openmes.webhook.event": (
                self.env["openmes.webhook.event"].sudo().create(
                    {"company_id": company.id, "event": "work_order.completed", "delivery_id": "d-19"}
                ),
                {"delivery_id": "d-19"},
            ),
            "openmes.material.lot.sent": (
                self.env["openmes.material.lot.sent"].sudo().create({
                    "company_id": company.id, "warehouse_id": warehouse.id,
                    "material_code": "FLOUR-01", "lot_number": "LOT-19",
                }),
                {"warehouse_id": warehouse.id, "material_code": "FLOUR-01", "lot_number": "LOT-19"},
            ),
            "openmes.stock.document": (
                self.env["openmes.stock.document"].sudo().create({"name": "MI/19", "mes_id": 1919}),
                {"mes_id": 1919},
            ),
        }

    def test_constraints_keep_their_names(self):
        for model, name, message in CONSTRAINTS:
            with self.subTest(model=model):
                conname = f"{self.env[model]._table}_{name}"
                self.env.cr.execute("SELECT contype FROM pg_constraint WHERE conname = %s", [conname])
                self.assertEqual(self.env.cr.fetchone(), ("u",), "same unique constraint as on Odoo 18")
                constraint = self.env["ir.model.constraint"].search([("name", "=", conname)])
                self.assertEqual(constraint.message, message)
                self.assertEqual(
                    constraint.get_external_id()[constraint.id], f"openmes_connector.constraint_{conname}"
                )

    def test_duplicate_is_refused_with_the_message(self):
        records = self._records()
        for model, _name, message in CONSTRAINTS:
            record, duplicate = records[model]
            with self.subTest(model=model):
                # Happy path: another value is accepted.
                other = {key: (value + "-B" if isinstance(value, str) else value) for key, value in duplicate.items()}
                if model == "openmes.stock.document":
                    other = {"mes_id": 1920}
                if model == "openmes.material.lot.sent":
                    other = dict(duplicate, lot_number="LOT-20")
                record.sudo().copy(other)
                self.env.flush_all()
                # The same value is refused by the database, with the module's message.
                with mute_logger("odoo.sql_db"), self.assertRaises(psycopg2.IntegrityError) as caught:
                    with self.env.cr.savepoint():
                        record.sudo().copy(duplicate)
                        self.env.flush_all()
                self.assertEqual(self.env[model]._sql_error_to_message(caught.exception), message)
