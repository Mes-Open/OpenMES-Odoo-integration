"""Odoo 20 API changes the module follows (one test per change).

- ``ir.model.access`` and ``ir.rule`` merged into ``ir.access``
  (``security/ir.access.csv``): group rows grant, rows without a group restrict.
- ``ir.config_parameter.get_param/set_param`` replaced by typed getters/setters
  (``get_str``/``set_str``…); ``set_str(key, False)`` keeps an empty record.
- ``mrp.production._action_cancel`` folded into ``action_cancel``.
- ``product_uom_id`` (``product_uom`` on ``stock.move``) renamed to ``uom_id`` on
  ``mrp.production``, ``mrp.bom``, ``mrp.bom.line``, ``stock.move`` and
  ``stock.move.line``.
"""

from odoo.tests import tagged
from odoo.tests.common import new_test_user

from .common import FakeResponse, OpenmesCase


@tagged("post_install", "-at_install")
class TestOdoo20Port(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._configure(order_source="manufacturing_orders")
        cls.bun = cls.env["product.product"].create({
            "name": "Bun", "default_code": "BUN-01", "is_storable": True, "openmes_manufacture": True,
        })
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.bun.product_tmpl_id.id,
            "product_qty": 10,
            "bom_line_ids": [(0, 0, {"product_id": cls.flour.id, "product_qty": 1})],
        })

    def _mo(self, qty=10):
        production = self.env["mrp.production"].create({
            "product_id": self.bun.id, "product_qty": qty, "uom_id": self.bun.uom_id.id,
        })
        production.action_confirm()
        return production

    @staticmethod
    def _sent(work_order, **vals):
        work_order.write(dict({"state": "sent", "mes_id": 77, "mes_status": "PENDING",
                               "sent_date": "2026-11-01 08:00:00"}, **vals))

    # ── ir.access ────────────────────────────────────────────────────────────

    def test_security_is_loaded_as_ir_access(self):
        rule = self.env.ref("openmes_connector.openmes_work_order_company_rule")
        self.assertEqual(rule._name, "ir.access")
        self.assertEqual((rule.model_id.model, rule.group_id, rule.operation, rule.kind),
                         ("openmes.work.order", self.env["res.groups"], "crud", "restriction"))
        self.assertIn("company_ids", rule.domain)
        grant = self.env.ref("openmes_connector.access_openmes_work_order_sale_manager")
        self.assertEqual((grant._name, grant.group_id, grant.operation, grant.kind),
                         ("ir.access", self.env.ref("sales_team.group_sale_manager"), "ru", "permission"))
        restrictions = self.env["ir.access"].search([
            ("model_id.model", "like", "openmes.%"), ("group_id", "=", False),
        ])
        self.assertEqual(len(restrictions), 8, "every former multi-company ir.rule is a restriction")

    def test_ir_access_rights_per_group(self):
        salesman = new_test_user(self.env, "o20_salesman", groups="base.group_user,sales_team.group_sale_salesman")
        manager = new_test_user(self.env, "o20_manager", groups="base.group_user,sales_team.group_sale_manager")
        admin = new_test_user(self.env, "o20_admin", groups="base.group_user,base.group_system")
        WorkOrder = self.env["openmes.work.order"]
        rights = {
            user.login: tuple(WorkOrder.with_user(user).has_access(op) for op in ("read", "write", "create", "unlink"))
            for user in (salesman, manager, admin)
        }
        self.assertEqual(rights, {
            "o20_salesman": (True, False, False, False),
            "o20_manager": (True, True, False, False),
            "o20_admin": (True, True, True, True),
        })
        self.assertFalse(self.env["openmes.sync.log"].with_user(manager).has_access("read"))

    def test_ir_access_restriction_hides_other_company_records(self):
        other = self.env["res.company"].create({"name": "Other bakery"})
        user = new_test_user(self.env, "o20_two_companies", groups="base.group_user,sales_team.group_sale_salesman",
                             company_id=self.env.company.id, company_ids=[(6, 0, [self.env.company.id, other.id])])
        Line = self.env["openmes.line"]
        theirs = Line.create({"code": "O20T", "name": "Theirs", "company_id": other.id})
        domain = [("id", "in", (self.line_l1 | theirs).ids)]
        Line = Line.with_user(user)
        self.assertEqual(Line.with_context(allowed_company_ids=[self.env.company.id]).search(domain), self.line_l1)
        self.assertEqual(Line.with_context(allowed_company_ids=[self.env.company.id, other.id]).search(domain),
                         self.line_l1 | theirs)
        self.assertFalse(Line.with_context(allowed_company_ids=[self.env.company.id]).browse(theirs.id)
                         .has_access("read"))

    # ── ir.config_parameter ──────────────────────────────────────────────────

    def test_legacy_parameters_are_deleted_not_emptied(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_str("openmes_connector.webhook_url", "https://odoo.example.test/openmes/webhook")
        icp.set_str("openmes_connector.order_prefix", "OLD")
        self.assertTrue(self.env["res.company"]._openmes_adopt_legacy_params())
        self.assertFalse(icp.search_count([("key", "in", ("openmes_connector.webhook_url",
                                                         "openmes_connector.order_prefix"))]))
        self.assertFalse(self.env["res.company"]._openmes_adopt_legacy_params(), "nothing left to adopt")

    def test_webhook_url_reads_base_url_with_get_str(self):
        self.env["ir.config_parameter"].sudo().set_str("web.base.url", "https://odoo.example.test/ ")
        settings = self.env["res.config.settings"].create({})
        self.assertEqual(settings.openmes_webhook_url, "https://odoo.example.test/openmes/webhook")

    # ── mrp.production.action_cancel ─────────────────────────────────────────

    def test_cancel_through_stock_moves_queues_the_cancellation(self):
        # Odoo 20 cancels the MO through action_cancel when its last component
        # move is cancelled (19 called the removed _action_cancel).
        production = self._mo()
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        production.move_raw_ids._action_cancel()
        self.assertEqual(production.state, "cancel")
        self.assertTrue(work_order.pending_cancel)
        response = FakeResponse(200, {"data": {"result": "cancelled", "work_order": {"id": 77}}})
        with self.mock_openmes(response) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertTrue(calls[0]["url"].endswith(f"/work-orders/{work_order.name}/cancel"))
        self.assertEqual(work_order.state, "cancelled")

    def test_cancel_unsent_mo_stays_local(self):
        production = self._mo()
        with self.mock_openmes():
            production.action_cancel()
        self.assertEqual(production.openmes_work_order_ids.state, "cancelled")

    # ── uom_id ───────────────────────────────────────────────────────────────

    def test_mo_unit_is_read_from_uom_id(self):
        production = self.env["mrp.production"].create({
            "product_id": self.bun.id, "product_qty": 2, "uom_id": self.uom_dozen.id,
        })
        production.action_confirm()
        self.assertEqual(production.openmes_work_order_ids.planned_qty, 24)

    def test_mo_unit_change_is_resent(self):
        production = self._mo(qty=2)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        production.uom_id = self.uom_dozen
        self.assertEqual((work_order.state, work_order.planned_qty), ("to_send", 24))

    def test_bom_units_are_read_from_uom_id(self):
        # common.py: 10 breads need 5 kg flour and 100 g (0.1 kg) yeast.
        bom = self.env["mrp.bom"].search([("product_tmpl_id", "=", self.bread.product_tmpl_id.id)])
        self.assertEqual(bom.bom_line_ids.filtered(lambda l: l.product_id == self.yeast).uom_id, self.uom_gram)
        components = self.env["openmes.sync"]._bom_components(self.bread, bom)
        self.assertAlmostEqual(components[self.flour], 0.5)
        self.assertAlmostEqual(components[self.yeast], 0.01)

    def test_stock_document_moves_get_uom_id(self):
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        warehouse.openmes_code = "0100"
        line = {"material_code": "FLOUR-01", "product_type_code": None, "lot_number": None,
                "quantity": 4, "unit_of_measure": "kg", "notes": None}
        doc = {"id": 91, "document_no": "MI/2026/0091", "type": "material_issue", "status": "posted",
               "direction": "out", "warehouse_code": "RAW-1", "warehouse_erp_code": "0100",
               "work_order_no": None, "erp_reference": None, "erp_synced_at": None,
               "posted_at": "2026-11-03T18:37:13+00:00", "lines": [line]}
        page = FakeResponse(200, {"data": [doc], "meta": {"next_cursor": None, "has_more": False}})
        with self.mock_openmes(page, FakeResponse(200, {"data": {"id": 1}})):
            self.env["openmes.stock.document"]._openmes_sync_documents()
        move = self.env["openmes.stock.document"].search([("mes_id", "=", 91)]).move_ids
        self.assertEqual(move.state, "done")
        self.assertEqual((move.uom_id, move.move_line_ids.uom_id), (self.uom_kg, self.uom_kg))
        self.assertEqual(move.quantity, 4)

    # ── Views ────────────────────────────────────────────────────────────────

    def test_every_module_view_loads(self):
        models = [name for name, model in self.env.registry.items()
                  if name.startswith("openmes.") and not model._abstract and not model._transient]
        self.assertGreaterEqual(len(models), 8)
        for name in models:
            with self.subTest(model=name):
                views = self.env[name].get_views([(False, "form"), (False, "list")])
                self.assertEqual(set(views["views"]), {"form", "list"})
        for name in ("mrp.production", "sale.order", "stock.warehouse", "mrp.workcenter", "product.template"):
            with self.subTest(model=name):
                self.env[name].get_views([(False, "form")])
