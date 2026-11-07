from unittest.mock import patch

import requests

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from .common import FakeResponse, OpenmesCase


def _settlement_data(**overrides):
    data = {
        "order_no": "WO",
        "status": "DONE",
        "planned_qty": 500,
        "produced_qty": 480,
        "consumption": [
            {
                "material_code": "FLOUR-01",
                "material_name": "Rye flour",
                "unit_of_measure": "kg",
                "consumed_qty": 241.5,
                "scrap_qty": 3.5,
                "returned_qty": 5,
                "status": "consumed",
                "lots": [{"lot_number": "LOT-2026-0815", "quantity": 250}],
            }
        ],
        "scrap": {"total_qty": 20, "entries": [{"quantity": 20, "reason": None, "step": None}]},
        "costs": {
            "currency": "PLN",
            "mixed_currency": False,
            "total_cost": 1393,
            "cost_per_unit": 2.9021,
            "materials": {"total": 483, "items": []},
            "labor": {"total": 870, "items": []},
            "additional": {"total": 40, "items": []},
        },
    }
    data.update(overrides)
    return data


def _settlement_body(**overrides):
    return FakeResponse(200, {"data": _settlement_data(**overrides)})


@tagged("post_install", "-at_install")
class TestSettlement(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.salesman = new_test_user(cls.env, "om_settle_salesman", groups="sales_team.group_sale_salesman")
        cls.sale_manager = new_test_user(cls.env, "om_settle_sale_manager", groups="sales_team.group_sale_manager")
        cls.mrp_user = new_test_user(cls.env, "om_settle_mrp_user", groups="mrp.group_mrp_user")
        cls.pln = cls.env.ref("base.PLN")
        cls.pln.active = True
        cls.eur = cls.env.ref("base.EUR")
        cls.eur.active = True
        cls.cron = cls.env.ref("openmes_connector.ir_cron_openmes_settlements")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _work_order(self, qty=60):
        order = self._create_order([(self.bread, qty, {})])
        order.user_id = self.salesman
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        work_order.write({
            "state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS", "mes_planned_qty": qty,
            "sent_date": "2026-11-01 08:00:00",
        })
        return work_order

    def _done_row(self, work_order, updated="2026-11-02T11:30:00+00:00"):
        return {"order_no": work_order.name, "id": work_order.mes_id, "status": "DONE",
                "produced_qty": work_order.planned_qty, "planned_qty": work_order.planned_qty,
                "completed_at": "2026-11-02T11:30:00+00:00", "updated_at": updated}

    def _done(self, work_order):
        with self.mock_openmes():
            work_order._openmes_apply_status(self._done_row(work_order))

    def _cron(self, *responses):
        with self.mock_openmes(*responses) as calls:
            self.env["openmes.work.order"]._cron_openmes_fetch_settlements()
        return calls

    def _settlement_messages(self, record):
        return record.message_ids.filtered(lambda m: "Settlement of OpenMES work order" in (m.body or ""))

    def _triggers(self):
        return self.env["ir.cron.trigger"].search_count([("cron_id", "=", self.cron.id)])

    # ── Marking pending ──────────────────────────────────────────────────────

    def test_done_from_pull_marks_pending_without_http(self):
        work_order = self._work_order()
        triggers = self._triggers()
        self._done(work_order)
        self.assertEqual(work_order.mes_status, "DONE")
        self.assertEqual(work_order.settlement_state, "pending")
        self.assertEqual(work_order.settlement_attempts, 0)
        self.assertGreater(self._triggers(), triggers, "the settlement cron is triggered")

    def test_done_from_webhook_marks_pending_without_http(self):
        work_order = self._work_order()
        with self.mock_openmes():
            handled = self.env["openmes.webhook"]._openmes_dispatch(
                "work_order.status_changed", self._done_row(work_order)
            )
        self.assertTrue(handled)
        self.assertEqual(work_order.settlement_state, "pending")

    def test_other_status_does_not_mark_pending(self):
        work_order = self._work_order()
        with self.mock_openmes():
            work_order._openmes_apply_status(dict(self._done_row(work_order), status="PAUSED"))
        self.assertFalse(work_order.settlement_state)

    # ── Fetching ─────────────────────────────────────────────────────────────

    def test_cron_fetches_and_stores_settlement(self):
        work_order = self._work_order()
        self._done(work_order)
        calls = self._cron(_settlement_body())
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].endswith("/api/v1/erp/work-orders/" + work_order.name), call["url"])
        self.assertEqual(call["params"], {"by": "order_no", "include": "consumption,scrap,costs"})

        self.assertEqual(work_order.settlement_state, "done")
        self.assertTrue(work_order.settlement_date)
        self.assertFalse(work_order.settlement_error)
        self.assertEqual(work_order.scrap_qty, 20)
        self.assertAlmostEqual(work_order.settlement_total_cost, 1393)
        self.assertAlmostEqual(work_order.settlement_cost_per_unit, 2.9021)
        self.assertEqual(work_order.settlement_currency_code, "PLN")
        self.assertEqual(work_order.settlement_currency_id, self.pln)
        self.assertFalse(work_order.settlement_mixed_currency)
        line = work_order.consumption_ids
        self.assertEqual(len(line), 1)
        self.assertEqual((line.material_code, line.material_name, line.unit_of_measure),
                         ("FLOUR-01", "Rye flour", "kg"))
        self.assertEqual(line.product_id, self.flour)
        self.assertEqual(line.lot_numbers, "LOT-2026-0815 (250)")
        self.assertEqual((line.consumed_qty, line.scrap_qty, line.returned_qty), (241.5, 3.5, 5))

        messages = self._settlement_messages(work_order.sale_order_id)
        self.assertEqual(len(messages), 1)
        self.assertIn("scrap 20", messages.body)
        self.assertIn("1393.00 PLN", messages.body)
        self.assertIn("2.9021 PLN per unit", messages.body)
        self.assertIn("1 material(s)", messages.body)

    def test_order_number_is_url_encoded(self):
        work_order = self._work_order()
        work_order.name = "ODOO/S00042/1"
        self._done(work_order)
        calls = self._cron(_settlement_body())
        self.assertTrue(calls[0]["url"].endswith("/work-orders/ODOO%2FS00042%2F1"), calls[0]["url"])
        self.assertEqual(work_order.settlement_state, "done")

    def test_unknown_material_has_no_product(self):
        work_order = self._work_order()
        self._done(work_order)
        consumption = [{"material_code": "SALT", "material_name": "Salt", "unit_of_measure": "g",
                        "consumed_qty": 10, "scrap_qty": None, "returned_qty": None, "lots": []}]
        self._cron(_settlement_body(consumption=consumption))
        line = work_order.consumption_ids
        self.assertFalse(line.product_id)
        self.assertFalse(line.lot_numbers)
        self.assertEqual((line.consumed_qty, line.scrap_qty, line.returned_qty), (10, 0, 0))

    def test_materials_are_matched_in_one_search(self):
        # One code names one product, one code two products, one code none.
        for name in ("Salt A", "Salt B"):
            self.env["product.product"].create({"name": name, "default_code": "SALT-01"})
        work_order = self._work_order()
        self._done(work_order)
        row = {"unit_of_measure": "kg", "consumed_qty": 1, "lots": []}
        consumption = [dict(row, material_code=code, material_name=code) for code in ("FLOUR-01", "SALT-01", "NOPE")]
        Product = type(self.env["product.product"])
        search = Product.search_fetch
        searches = []

        def spy(records, domain, *args, **kwargs):
            if any(isinstance(term, (list, tuple)) and term[0] == "default_code" for term in domain):
                searches.append(domain)
            return search(records, domain, *args, **kwargs)

        with patch.object(Product, "search_fetch", spy):
            self._cron(_settlement_body(consumption=consumption))
        self.assertEqual(len(searches), 1, "one product search for every consumption line")
        lines = {line.material_code: line.product_id for line in work_order.consumption_ids}
        self.assertEqual(lines, {"FLOUR-01": self.flour, "SALT-01": self.env["product.product"],
                                 "NOPE": self.env["product.product"]})

    def test_summary_is_posted_on_the_manufacturing_order(self):
        self._configure(order_source="manufacturing_orders")
        bun = self.env["product.product"].create({
            "name": "Bun", "default_code": "BUN-01", "is_storable": True, "openmes_manufacture": True,
        })
        self.env["mrp.bom"].create({
            "product_tmpl_id": bun.product_tmpl_id.id,
            "product_qty": 10,
            "bom_line_ids": [(0, 0, {"product_id": self.flour.id, "product_qty": 1})],
        })
        production = self.env["mrp.production"].create({"product_id": bun.id, "product_qty": 10})
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 78, "mes_status": "IN_PROGRESS",
                          "sent_date": "2026-11-01 08:00:00"})
        self._done(work_order)
        self.assertEqual(production.state, "done")
        self.assertEqual(work_order.settlement_state, "pending")
        self._cron(_settlement_body())
        self.assertEqual(work_order.settlement_state, "done")
        self.assertEqual(len(self._settlement_messages(production)), 1)

    def test_no_duplicates(self):
        work_order = self._work_order()
        self._done(work_order)
        self._cron(_settlement_body())
        order = work_order.sale_order_id
        self.assertEqual(len(self._settlement_messages(order)), 1)
        # The cron again, the same DONE from the pull and from a webhook.
        self._cron()
        with self.mock_openmes():
            work_order._openmes_apply_status(self._done_row(work_order, "2026-11-02T12:00:00+00:00"))
            self.env["openmes.webhook"]._openmes_dispatch(
                "work_order.status_changed", self._done_row(work_order, "2026-11-02T12:30:00+00:00")
            )
        self.assertEqual(work_order.settlement_state, "done")
        self._cron()
        self.assertEqual(len(work_order.consumption_ids), 1)
        self.assertEqual(len(self._settlement_messages(order)), 1)
        with self.assertRaises(UserError):
            work_order.with_user(self.sale_manager).action_openmes_fetch_settlement()

    # ── Errors and retries ───────────────────────────────────────────────────

    def test_http_error_then_retry(self):
        work_order = self._work_order()
        self._done(work_order)
        self._cron(FakeResponse(500, {"message": "Server Error"}))
        self.assertEqual(work_order.settlement_state, "error")
        self.assertEqual(work_order.settlement_attempts, 1)
        self.assertIn("HTTP 500", work_order.settlement_error)
        self.assertFalse(work_order.consumption_ids)
        self.assertFalse(self._settlement_messages(work_order.sale_order_id))
        self.assertEqual(work_order.mes_status, "DONE", "the status is kept")

        self._cron(_settlement_body())
        self.assertEqual(work_order.settlement_state, "done")
        self.assertFalse(work_order.settlement_error)
        self.assertEqual(work_order.settlement_attempts, 0)
        self.assertEqual(len(self._settlement_messages(work_order.sale_order_id)), 1)

    def test_timeout_and_client_errors(self):
        work_order = self._work_order()
        self._done(work_order)

        def timeout(*args, **kwargs):
            raise requests.Timeout("read timed out")

        self._cron(timeout)
        self.assertEqual(work_order.settlement_state, "error")
        self.assertIn("Cannot reach OpenMES", work_order.settlement_error)
        self._cron(FakeResponse(404, {"message": "Not found"}))
        self.assertIn("endpoint not found", work_order.settlement_error)
        self._cron(FakeResponse(403, {"message": "Forbidden"}))
        self.assertIn("lacks the scope", work_order.settlement_error)
        self.assertEqual(work_order.settlement_attempts, 3)
        self.assertNotIn("omk_", work_order.settlement_error)

    def test_retries_stop_after_five_failures_but_the_button_works(self):
        work_order = self._work_order()
        self._done(work_order)
        for _attempt in range(5):
            self._cron(FakeResponse(503, None))
        self.assertEqual(work_order.settlement_attempts, 5)
        self._cron()  # no call any more
        self.assertEqual(work_order.settlement_state, "error")
        with self.mock_openmes(_settlement_body()):
            action = work_order.with_user(self.sale_manager).action_openmes_fetch_settlement()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(work_order.settlement_state, "done")

    def test_button_reports_failure(self):
        work_order = self._work_order()
        self._done(work_order)
        with self.mock_openmes(FakeResponse(500, None)):
            action = work_order.with_user(self.sale_manager).action_openmes_fetch_settlement()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertEqual(work_order.settlement_state, "error")

    def test_button_on_order_finished_before_upgrade(self):
        work_order = self._work_order()
        work_order.mes_status = "DONE"  # finished before settlements existed
        self.assertFalse(work_order.settlement_state)
        with self.mock_openmes(_settlement_body()):
            work_order.with_user(self.sale_manager).action_openmes_fetch_settlement()
        self.assertEqual(work_order.settlement_state, "done")

    def test_order_without_consumption_or_scrap(self):
        work_order = self._work_order()
        self._done(work_order)
        self._cron(_settlement_body(
            consumption=[],
            scrap={"total_qty": 0, "entries": []},
            costs={"currency": "PLN", "mixed_currency": False, "total_cost": 0, "cost_per_unit": None},
        ))
        self.assertEqual(work_order.settlement_state, "done")
        self.assertFalse(work_order.consumption_ids)
        self.assertEqual(work_order.scrap_qty, 0)
        self.assertEqual(work_order.settlement_total_cost, 0)
        self.assertEqual(work_order.settlement_cost_per_unit, 0)
        self.assertIn("0 material(s)", self._settlement_messages(work_order.sale_order_id).body)

    def test_old_openmes_without_sections(self):
        work_order = self._work_order()
        self._done(work_order)
        data = {key: value for key, value in _settlement_data().items()
                if key not in ("consumption", "scrap", "costs")}
        self._cron(FakeResponse(200, {"data": data}))
        self.assertEqual(work_order.settlement_state, "error")
        self.assertIn("did not return settlement data", work_order.settlement_error)

    def test_status_no_longer_done(self):
        work_order = self._work_order()
        self._done(work_order)
        self._cron(_settlement_body(status="IN_PROGRESS"))
        self.assertEqual(work_order.settlement_state, "error")
        self.assertIn("no longer reports", work_order.settlement_error)
        self.assertFalse(work_order.consumption_ids)

    def test_malformed_values_write_nothing(self):
        work_order = self._work_order()
        self._done(work_order)
        costs = dict(_settlement_data()["costs"], total_cost="abc")
        self._cron(_settlement_body(costs=costs))
        self.assertEqual(work_order.settlement_state, "error")
        self.assertIn("invalid settlement data", work_order.settlement_error)
        self.assertEqual(work_order.settlement_attempts, 1)
        self.assertFalse(work_order.consumption_ids)
        self.assertEqual(work_order.scrap_qty, 0)
        self.assertFalse(work_order.settlement_currency_code)

        consumption = [dict(_settlement_data()["consumption"][0], consumed_qty="lots")]
        self._cron(_settlement_body(consumption=consumption))
        self.assertEqual(work_order.settlement_state, "error")
        self.assertFalse(work_order.consumption_ids)
        self.assertEqual(work_order.settlement_attempts, 2)

    # ── Currency ─────────────────────────────────────────────────────────────

    def test_mixed_currency(self):
        work_order = self._work_order()
        self._done(work_order)
        costs = dict(_settlement_data()["costs"], currency="EUR", mixed_currency=True)
        self._cron(_settlement_body(costs=costs))
        self.assertEqual(work_order.settlement_currency_code, "EUR")
        self.assertEqual(work_order.settlement_currency_id, self.eur)
        self.assertTrue(work_order.settlement_mixed_currency)
        body = self._settlement_messages(work_order.sale_order_id).body
        self.assertIn("1393.00 EUR", body)
        self.assertIn("summed without conversion", body)

    def test_unknown_currency(self):
        work_order = self._work_order()
        self._done(work_order)
        costs = dict(_settlement_data()["costs"], currency="XYZ")
        self._cron(_settlement_body(costs=costs))
        self.assertEqual(work_order.settlement_state, "done")
        self.assertEqual(work_order.settlement_currency_code, "XYZ")
        self.assertFalse(work_order.settlement_currency_id)

    # ── Access ───────────────────────────────────────────────────────────────

    def test_access_rights(self):
        work_order = self._work_order()
        self._done(work_order)
        self._cron(_settlement_body())
        work_order.settlement_state = "error"  # so the button would have work to do
        for user in (self.salesman, self.mrp_user):
            lines = work_order.with_user(user).consumption_ids
            self.assertEqual(lines.material_code, "FLOUR-01")
            with self.assertRaises(AccessError):
                work_order.with_user(user).action_openmes_fetch_settlement()
            with self.assertRaises(AccessError):
                lines.with_user(user).write({"consumed_qty": 1})

    def test_button_on_unfinished_order(self):
        work_order = self._work_order()
        with self.assertRaises(UserError):
            work_order.with_user(self.sale_manager).action_openmes_fetch_settlement()

    def test_multi_company(self):
        work_order = self._work_order()
        self._done(work_order)
        self._cron(_settlement_body())
        company_b = self.env["res.company"].create({"name": "Bakery B"})
        user_b = new_test_user(self.env, "om_settle_user_b", groups="sales_team.group_sale_salesman",
                               company_id=company_b.id, company_ids=[(6, 0, company_b.ids)])
        Consumption = self.env["openmes.work.order.consumption"].with_user(user_b)
        self.assertFalse(Consumption.search([("work_order_id", "=", work_order.id)]))
        self.assertTrue(self.env["openmes.work.order.consumption"].search([("work_order_id", "=", work_order.id)]))

    def test_disabled_integration_does_nothing(self):
        work_order = self._work_order()
        self._done(work_order)
        self._configure(enabled=False)
        self._cron()
        self.assertEqual(work_order.settlement_state, "pending")
