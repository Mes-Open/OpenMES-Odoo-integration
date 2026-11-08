from unittest.mock import patch

import requests

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from .common import FakeResponse, OpenmesCase
from .test_settlement import _settlement_data

LABOUR, ADDITIONAL, MATERIALS = 870.0, 40.0, 483.0


@tagged("post_install", "-at_install")
class TestMoCost(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._configure(order_source="manufacturing_orders", mo_extra_cost=True)
        cls.currency = cls.env.company.currency_id
        cls.other_currency = cls.env.ref("base.EUR" if cls.currency.name != "EUR" else "base.PLN")
        cls.other_currency.active = True
        cls.bun = cls.env["product.product"].create({
            "name": "Bun", "default_code": "BUN-01", "is_storable": True, "openmes_manufacture": True,
        })
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.bun.product_tmpl_id.id,
            "product_qty": 10,
            "bom_line_ids": [(0, 0, {"product_id": cls.flour.id, "product_qty": 1})],
        })
        cls.mrp_user = new_test_user(cls.env, "om_cost_mrp_user", groups="mrp.group_mrp_user")
        cls.mrp_manager = new_test_user(cls.env, "om_cost_mrp_manager", groups="mrp.group_mrp_manager")
        cls.salesman = new_test_user(cls.env, "om_cost_salesman", groups="sales_team.group_sale_salesman")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _mo(self, qty=10, product=None, uom=None):
        product = product or self.bun
        production = self.env["mrp.production"].create({
            # Odoo 20: mrp.bom(.line), mrp.production and mrp.workcenter.capacity renamed product_uom_id to uom_id.
            "product_id": product.id, "product_qty": qty, "uom_id": (uom or product.uom_id).id,
        })
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS",
                          "sent_date": "2026-11-01 08:00:00"})
        return production, work_order

    @staticmethod
    def _done_row(work_order, produced):
        return {"order_no": work_order.name, "id": work_order.mes_id, "status": "DONE",
                "produced_qty": produced, "planned_qty": work_order.planned_qty,
                "completed_at": "2026-11-02T11:30:00+00:00", "updated_at": "2026-11-02T11:30:00+00:00"}

    def _costs(self, labor=LABOUR, additional=ADDITIONAL, currency=None, mixed=False):
        return {
            "currency": currency or self.currency.name,
            "mixed_currency": mixed,
            "total_cost": MATERIALS + labor + additional,
            "cost_per_unit": 1.0,
            "materials": {"total": MATERIALS, "items": []},
            "labor": {"total": labor, "items": []},
            "additional": {"total": additional, "items": []},
        }

    def _body(self, work_order, produced=10, **costs):
        return FakeResponse(200, {"data": _settlement_data(
            order_no=work_order.name, produced_qty=produced, costs=self._costs(**costs))})

    def _done(self, work_order, produced=10, *responses):
        with self.mock_openmes(*responses) as calls:
            work_order._openmes_apply_status(self._done_row(work_order, produced))
        return calls

    def _cron(self, *responses):
        with self.mock_openmes(*responses) as calls:
            self.env["openmes.work.order"]._cron_openmes_fetch_settlements()
        return calls

    @staticmethod
    def _notes(production, text):
        return production.message_ids.filtered(lambda m: text in (m.body or ""))

    def _added(self, production):
        return self._notes(production, "were added to the manufacturing order cost")

    def _not_added(self, production):
        return self._notes(production, "were not added to the manufacturing order cost")

    def _close_by_hand(self, production, qty=10, user=None):
        manual = production.with_user(user) if user else production
        manual.qty_producing = qty
        manual._set_qty_producing()
        return manual.button_mark_done()

    # ── Option off ───────────────────────────────────────────────────────────

    def test_option_off_changes_nothing(self):
        self._configure(mo_extra_cost=False)
        production, work_order = self._mo()
        self.assertFalse(self._done(work_order, 10), "no HTTP in the close")
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
        self.assertEqual(work_order.settlement_state, "pending")
        self.assertFalse(self._added(production) or self._not_added(production))

    def test_option_default_is_off(self):
        company = self.env["res.company"].create({"name": "Fresh company"})
        self.assertFalse(company.openmes_mo_extra_cost)

    # ── Option on ────────────────────────────────────────────────────────────

    def test_cost_is_set_before_closing(self):
        production, work_order = self._mo()
        calls = self._done(work_order, 10, self._body(work_order))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertTrue(calls[0]["url"].endswith("/api/v1/erp/work-orders/" + work_order.name), calls[0]["url"])
        self.assertEqual(calls[0]["params"], {"by": "order_no", "include": "consumption,scrap,costs"})
        self.assertEqual(production.state, "done")
        self.assertAlmostEqual(production.extra_cost, (LABOUR + ADDITIONAL) / 10)
        self.assertEqual(work_order.settlement_state, "done")
        self.assertEqual(len(self._notes(production, "Settlement of OpenMES work order")), 1)
        added = self._added(production)
        self.assertEqual(len(added), 1)
        self.assertIn("91.0000", added.body)
        self.assertIn("Material costs from OpenMES are not added", added.body)
        self.assertFalse(self._cron(), "the cron has nothing left to fetch")

    def test_settlement_cost_sections_are_stored(self):
        production, work_order = self._mo()
        self._done(work_order, 10, self._body(work_order))
        self.assertAlmostEqual(work_order.settlement_labor_cost, LABOUR)
        self.assertAlmostEqual(work_order.settlement_additional_cost, ADDITIONAL)
        self.assertAlmostEqual(work_order.settlement_material_cost, MATERIALS)

    def test_valuation_includes_the_cost(self):
        fifo = self.env["product.category"].create({"name": "FIFO buns", "property_cost_method": "fifo"})
        self.bun.categ_id = fifo
        production, work_order = self._mo()
        self._done(work_order, 10, self._body(work_order))
        self.assertEqual(production.state, "done")
        # Odoo 19 values moves directly (stock.move.value).
        # Odoo 20: component (outgoing) moves are valued negative; _cal_price takes abs().
        components = abs(sum(production.move_raw_ids.mapped("value")))
        self.assertTrue(components)
        finished = production.move_finished_ids.filtered(lambda m: m.product_id == self.bun)
        value = sum(finished.mapped("value"))
        self.assertAlmostEqual(value, components + LABOUR + ADDITIONAL, places=2)

    def test_under_produced_divides_by_the_closed_quantity(self):
        production, work_order = self._mo()
        self._done(work_order, 8, self._body(work_order, produced=8))
        self.assertEqual(production.state, "done")
        self.assertAlmostEqual(production.extra_cost, (LABOUR + ADDITIONAL) / 8)
        backorder = production._openmes_backorder_family() - production
        self.assertEqual(len(backorder), 1)
        self.assertEqual(backorder.extra_cost, 0)

    def test_cost_is_per_product_unit(self):
        production, work_order = self._mo(qty=1, uom=self.uom_dozen)
        self._done(work_order, 12, self._body(work_order, produced=12))
        self.assertEqual(production.state, "done")
        self.assertAlmostEqual(production.extra_cost, (LABOUR + ADDITIONAL) / 12)

    def test_existing_cost_is_replaced(self):
        production, work_order = self._mo()
        production.extra_cost = 5
        self._done(work_order, 10, self._body(work_order))
        self.assertAlmostEqual(production.extra_cost, 91)
        self.assertIn("It replaces the previous Extra Unit Cost of 5.0000", self._added(production).body)

    def test_zero_cost(self):
        production, work_order = self._mo()
        self._done(work_order, 10, self._body(work_order, labor=0, additional=0))
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
        self.assertEqual(len(self._added(production)), 1)

    # ── Currency ─────────────────────────────────────────────────────────────

    def test_other_currency_is_not_added(self):
        production, work_order = self._mo()
        self._done(work_order, 10, self._body(work_order, currency=self.other_currency.name))
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
        note = self._not_added(production)
        self.assertEqual(len(note), 1)
        self.assertIn(self.other_currency.name, note.body)
        self.assertIn(self.currency.name, note.body)

    def test_mixed_currency_is_not_added(self):
        production, work_order = self._mo()
        self._done(work_order, 10, self._body(work_order, mixed=True))
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
        self.assertIn("several currencies", self._not_added(production).body)

    def test_unknown_currency_is_not_added(self):
        production, work_order = self._mo()
        self._done(work_order, 10, self._body(work_order, currency="XYZ"))
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
        self.assertIn("XYZ", self._not_added(production).body)

    # ── OpenMES errors ───────────────────────────────────────────────────────

    def test_fetch_errors_close_without_the_cost(self):
        def timeout(*args, **kwargs):
            raise requests.Timeout("read timed out")

        for answer in (FakeResponse(500, {"message": "Server Error"}), timeout, FakeResponse(404, None)):
            with self.subTest(answer=answer):
                production, work_order = self._mo()
                self._done(work_order, 10, answer)
                self.assertEqual(production.state, "done")
                self.assertEqual(production.extra_cost, 0)
                note = self._not_added(production)
                self.assertEqual(len(note), 1)
                self.assertIn("could not be fetched", note.body)
                self.assertNotIn("omk_", note.body)
                self.assertEqual(work_order.settlement_state, "pending")
                # The settlement arrives later: the closed MO is not revalued.
                self._cron(self._body(work_order))
                self.assertEqual(work_order.settlement_state, "done")
                self.assertEqual(production.extra_cost, 0)

    def test_invalid_settlement_closes_without_the_cost(self):
        bad_total = dict(self._costs(), total_cost="abc")
        bad_labor = dict(self._costs(), labor=[870])
        for costs in (bad_total, bad_labor):
            with self.subTest(costs=costs):
                production, work_order = self._mo()
                body = FakeResponse(200, {"data": _settlement_data(order_no=work_order.name, costs=costs)})
                with self.assertLogs("odoo.addons.openmes_connector.models.openmes_work_order_settlement",
                                     "WARNING"):
                    self._done(work_order, 10, body)
                self.assertEqual(production.state, "done")
                self.assertEqual(production.extra_cost, 0)
                self.assertIn("invalid settlement data", self._not_added(production).body)

    def test_old_openmes_without_sections(self):
        production, work_order = self._mo()
        data = {key: value for key, value in _settlement_data(order_no=work_order.name).items()
                if key not in ("consumption", "scrap", "costs")}
        self._done(work_order, 10, FakeResponse(200, {"data": data}))
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
        self.assertEqual(len(self._not_added(production)), 1)

    # ── Edge cases ───────────────────────────────────────────────────────────

    def test_without_extra_cost_field(self):
        production, work_order = self._mo()
        with patch.object(type(self.env["mrp.production"]), "_openmes_has_extra_cost", return_value=False):
            self.assertFalse(self._done(work_order, 10))
        self.assertEqual(production.state, "done")
        self.assertEqual(work_order.settlement_state, "pending")
        self.assertFalse(self._added(production) or self._not_added(production))
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(settings.openmes_mo_extra_cost_available)
        with patch.object(type(self.env["mrp.production"]), "_openmes_has_extra_cost", return_value=False):
            self.assertFalse(self.env["res.config.settings"].create({}).openmes_mo_extra_cost_available)

    def test_sale_lines_mode_unchanged(self):
        self._configure(order_source="sale_lines")
        order = self._create_order([(self.bread, 10, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 79, "mes_status": "IN_PROGRESS"})
        self.assertFalse(self._done(work_order, 10), "no HTTP for a sale line work order")
        self.assertEqual(work_order.settlement_state, "pending")

    def test_mo_outside_openmes_closed_by_hand(self):
        self._configure(order_source="sale_lines")
        production = self.env["mrp.production"].create({"product_id": self.bun.id, "product_qty": 10})
        production.action_confirm()
        self.assertFalse(production.openmes_work_order_ids)
        production.extra_cost = 3
        with self.mock_openmes():
            self._close_by_hand(production)
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 3)

    def test_failed_close_then_retry_uses_the_stored_settlement(self):
        production, work_order = self._mo()
        original = type(production).button_mark_done
        fail = [True]

        def button_mark_done(productions):
            result = original(productions)
            if fail[0]:
                raise ValueError("boom")
            return result

        with patch.object(type(production), "button_mark_done", button_mark_done), \
                self.assertLogs("odoo.addons.openmes_connector.models.openmes_work_order", "ERROR"):
            self._done(work_order, 10, self._body(work_order))
        self.assertNotEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
        self.assertEqual(work_order.settlement_state, "pending", "the fetched settlement was rolled back")
        self.assertFalse(self._added(production))

        self._cron(self._body(work_order))
        self.assertEqual(work_order.settlement_state, "done")
        with self.mock_openmes() as calls:
            work_order.with_user(self.mrp_manager).action_openmes_finish_production()
        self.assertFalse(calls)
        self.assertEqual(production.state, "done")
        self.assertAlmostEqual(production.extra_cost, 91)
        self.assertEqual(len(self._added(production)), 1)

    def test_manual_close_by_mrp_user_with_stored_settlement(self):
        production, work_order = self._mo()
        work_order.write({"mes_status": "DONE", "produced_qty": 10})
        self._cron()  # not pending: nothing to fetch
        with self.mock_openmes(self._body(work_order)):
            work_order._openmes_fetch_settlement()
        self.assertEqual(work_order.settlement_state, "done")
        with self.mock_openmes() as calls:
            self._close_by_hand(production, user=self.mrp_user)
        self.assertFalse(calls)
        self.assertEqual(production.state, "done")
        self.assertAlmostEqual(production.extra_cost, 91)

    def test_manual_close_before_openmes_finished(self):
        production, work_order = self._mo()
        with self.mock_openmes() as calls:
            self._close_by_hand(production, user=self.mrp_user)
        self.assertFalse(calls)
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
        self.assertIn("is not finished yet", self._not_added(production).body)

    def test_consumption_wizard_round_trip_fetches_once(self):
        production, work_order = self._mo()
        work_order.write({"mes_status": "DONE", "produced_qty": 10})
        production.qty_producing = 10
        production._set_qty_producing()
        production.move_raw_ids.quantity = 5  # 5 kg instead of 1: Odoo warns
        production.move_raw_ids.picked = True
        with self.mock_openmes(self._body(work_order)) as calls:
            action = production.button_mark_done()
            self.assertIsInstance(action, dict, "the consumption warning is shown")
            self.assertFalse(calls, "no HTTP before the close is confirmed")
            self.assertEqual(action["res_model"], "mrp.consumption.warning")
            wizard = self.env["mrp.consumption.warning"].with_context(**action["context"]).create({})
            wizard.action_confirm()
        self.assertEqual(len(calls), 1)
        self.assertEqual(production.state, "done")
        self.assertAlmostEqual(production.extra_cost, 91)
        self.assertEqual(len(self._added(production)), 1)

    # ── Access and companies ─────────────────────────────────────────────────

    def test_option_is_admin_only(self):
        for user in (self.salesman, self.mrp_manager, self.mrp_user):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.env.company.with_user(user).read(["openmes_mo_extra_cost"])

    def test_settings_write_the_current_company_only(self):
        other = self.env["res.company"].create({"name": "Other plant"})
        self._configure(mo_extra_cost=False)
        self.env["res.config.settings"].create({"openmes_mo_extra_cost": True}).execute()
        self.assertTrue(self.env.company.openmes_mo_extra_cost)
        self.assertFalse(other.openmes_mo_extra_cost)

    def test_other_company_without_the_option(self):
        company_b = self.env["res.company"].create({"name": "Plant B"})
        self.env.user.company_ids |= company_b
        line_b = self.env["openmes.line"].create({"code": "L1", "name": "B one", "company_id": company_b.id})
        self._configure(company_b, enabled=True, api_url="https://mes-b.example.test", api_key="omk_" + "b" * 40,
                        default_line_id=line_b.id, order_source="manufacturing_orders", mo_extra_cost=False)
        self.assertTrue(self.env.company.openmes_mo_extra_cost, "on for company A")
        production = self.env["mrp.production"].with_company(company_b).create({
            "product_id": self.bun.id, "product_qty": 10, "company_id": company_b.id,
        })
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        self.assertEqual(work_order.company_id, company_b)
        work_order.write({"state": "sent", "mes_id": 80, "mes_status": "IN_PROGRESS"})
        self.assertFalse(self._done(work_order, 10))
        self.assertEqual(production.state, "done")
        self.assertEqual(production.extra_cost, 0)
