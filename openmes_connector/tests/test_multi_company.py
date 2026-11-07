"""Settings per company: every company talks to its own OpenMES instance."""

import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from unittest.mock import patch

import requests

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from .common import API_KEY, API_URL, REQUEST_PATH, FakeResponse, OpenmesCase, import_report

URL_B = "https://mes-b.example.test"
KEY_B = "omk_" + "b" * 40
SECRET_A = "Aa1" + "a" * 37
SECRET_B = "Bb2" + "b" * 37


def page(rows=(), next_cursor=None):
    return FakeResponse(200, {"data": list(rows), "meta": {"next_cursor": next_cursor}})


@tagged("post_install", "-at_install")
class TestMultiCompany(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.env.ref("base.EUR").active = True
        cls.company_b = cls.env["res.company"].create({
            "name": "Plant B",
            "currency_id": cls.env.ref("base.EUR").id,
        })
        cls.env.user.company_ids |= cls.company_b
        # Same code as company A's line: two OpenMES instances, two "L1".
        cls.line_b1 = cls.env["openmes.line"].create({"code": "L1", "name": "B one", "company_id": cls.company_b.id})
        cls.company_b.write({
            "openmes_enabled": True,
            "openmes_api_url": URL_B,
            "openmes_api_key": KEY_B,
            "openmes_order_prefix": "BBB",
            "openmes_default_line_id": cls.line_b1.id,
        })
        cls.wh_a = cls.env["stock.warehouse"].search([("company_id", "=", cls.company_a.id)], limit=1)
        cls.wh_b = cls.env["stock.warehouse"].search([("company_id", "=", cls.company_b.id)], limit=1)
        cls.wh_a.openmes_code = "WA"
        cls.wh_b.openmes_code = "WB"
        # The bread BoM is shared, like the bread itself.
        cls.env["mrp.bom"].search([("product_tmpl_id", "=", cls.bread.product_tmpl_id.id)]).company_id = False
        cls.bagel_a = cls.env["product.product"].create({
            "name": "Bagel", "default_code": "BAGEL-A", "openmes_manufacture": True, "company_id": cls.company_a.id,
        })
        cls.bun_b = cls.env["product.product"].create({
            "name": "Bun", "default_code": "BUN-B", "openmes_manufacture": True, "is_storable": True,
            "company_id": cls.company_b.id,
        })

    # ── Helpers ──────────────────────────────────────────────────────────────

    @contextmanager
    def route(self, handler=None):
        """Patch HTTP; ``handler(call)`` answers each request (default: an
        empty but valid answer to everything)."""
        calls = []

        def fake_request(method, url, **kwargs):
            call = dict(kwargs, method=method, url=url)
            call["company"] = "B" if url.startswith(URL_B) else "A"
            call["path"] = url.split("/api/v1/erp", 1)[1]
            calls.append(call)
            answer = (handler or self._default_answer)(call)
            if answer is None:
                answer = self._default_answer(call)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch(REQUEST_PATH, side_effect=fake_request):
            yield calls

    @staticmethod
    def _default_answer(call):
        if call["method"] == "POST" and call["path"].endswith("/import"):
            return import_report(imported=1)
        if call["method"] == "POST":
            return FakeResponse(200, {"data": {}})
        return page()

    def _order(self, company, product=None, qty=2):
        order = self.env["sale.order"].with_company(company).create({
            "partner_id": self.partner.id,
            "company_id": company.id,
            "order_line": [(0, 0, {"product_id": (product or self.bread).id, "product_uom_qty": qty})],
        })
        order.action_confirm()
        return order

    def _work_order(self, company, **vals):
        work_order = self._order(company).openmes_work_order_ids
        self.assertEqual(len(work_order), 1)
        if vals:
            work_order.write(vals)
        return work_order

    def _sent(self, company, **vals):
        return self._work_order(company, **dict(
            {"state": "sent", "mes_status": "PENDING", "sent_date": "2026-11-01 08:00:00"}, **vals
        ))

    @staticmethod
    def _by_company(calls, path=None):
        result = {"A": [], "B": []}
        for call in calls:
            if path is None or call["path"].startswith(path):
                result[call["company"]].append(call)
        return result

    # ── Work orders go to the instance of their company ──────────────────────

    def test_send_cron_routes_each_company(self):
        wo_a = self._work_order(self.company_a)
        wo_b = self._work_order(self.company_b)
        self.assertTrue(wo_a.name.startswith("ODOO-"))
        self.assertTrue(wo_b.name.startswith("BBB-"))
        self.assertEqual(wo_b.company_id, self.company_b)

        with self.route() as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        sent = self._by_company(calls, "/work-orders/import")
        self.assertEqual(len(sent["A"]), 1)
        self.assertEqual(len(sent["B"]), 1)
        self.assertEqual(sent["A"][0]["headers"]["X-Api-Key"], API_KEY)
        self.assertEqual(sent["B"][0]["headers"]["X-Api-Key"], KEY_B)
        self.assertEqual([o["order_no"] for o in sent["A"][0]["json"]["orders"]], [wo_a.name])
        self.assertEqual([o["order_no"] for o in sent["B"][0]["json"]["orders"]], [wo_b.name])
        self.assertEqual((wo_a.state, wo_b.state), ("sent", "sent"))

    def test_order_source_per_company(self):
        self._configure(self.company_b, order_source="manufacturing_orders")
        self.assertTrue(self._order(self.company_a).openmes_work_order_ids, "A: sale lines")
        self.assertFalse(self._order(self.company_b).openmes_work_order_ids, "B: not from sale lines")
        production = self.env["mrp.production"].with_company(self.company_b).create({
            "product_id": self.bun_b.id, "product_qty": 3, "company_id": self.company_b.id,
        })
        production.action_confirm()
        work_order = self.env["openmes.work.order"].search([("production_id", "=", production.id)])
        self.assertEqual(len(work_order), 1)
        self.assertTrue(work_order.name.startswith("BBB-"))
        self.assertEqual((work_order.company_id, work_order.line_code), (self.company_b, "L1"))

    def test_disabled_company_creates_and_sends_nothing(self):
        stale = self._work_order(self.company_b)
        self._configure(self.company_b, enabled=False)
        self.assertFalse(self._order(self.company_b).openmes_work_order_ids)
        self._work_order(self.company_a)
        with self.route() as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual({call["company"] for call in calls}, {"A"})
        self.assertEqual(stale.state, "to_send")

    def test_one_failing_instance_does_not_stop_the_other(self):
        cases = {
            "500": lambda call: FakeResponse(500, None),
            "401": lambda call: FakeResponse(401, {"message": "Unauthenticated."}),
            "timeout": lambda call: requests.Timeout("timed out"),
        }
        for name, failure in cases.items():
            with self.subTest(case=name):
                wo_a = self._work_order(self.company_a)
                wo_b = self._work_order(self.company_b)
                with self.route(lambda call: failure(call) if call["company"] == "B" else None):
                    self.env["openmes.work.order"]._cron_openmes_send()
                self.assertEqual(wo_a.state, "sent")
                self.assertEqual(wo_b.state, "error")
                self.assertTrue(wo_b.last_error)
                wo_b.action_cancel()

    def test_manual_send_spanning_companies(self):
        wo_a = self._work_order(self.company_a)
        wo_b = self._work_order(self.company_b)
        with self.route() as calls:
            (wo_a | wo_b).action_send()
        self.assertEqual(sorted(call["company"] for call in calls), ["A", "B"])
        self.assertEqual((wo_a.state, wo_b.state), ("sent", "sent"))

        wo_a2 = self._work_order(self.company_a)
        wo_b2 = self._work_order(self.company_b)
        self._configure(self.company_b, enabled=False, api_key=False)
        with self.route() as calls:
            (wo_a2 | wo_b2).action_send()
        self.assertEqual([call["company"] for call in calls], ["A"])
        self.assertEqual(wo_a2.state, "sent")
        self.assertEqual(wo_b2.state, "error")
        self.assertIn("not configured for company Plant B", wo_b2.last_error)

    def test_cancellation_goes_to_the_owning_instance(self):
        wo_b = self._sent(self.company_b, mes_id=5)
        wo_b.sale_order_id._action_cancel()
        self.assertTrue(wo_b.pending_cancel)
        answer = FakeResponse(200, {"data": {"result": "cancelled", "work_order": {"id": 5}}})
        with self.route(lambda call: answer) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual([(c["company"], c["path"]) for c in calls], [("B", f"/work-orders/{wo_b.name}/cancel")])
        self.assertEqual(wo_b.state, "cancelled")

    # ── Pulling state back, per company ──────────────────────────────────────

    def test_status_pull_per_company(self):
        wo_a = self._sent(self.company_a)
        wo_b = self._sent(self.company_b)

        def answer(call):
            if not call["path"].startswith("/production/completions"):
                return None
            if call["company"] == "A":
                return page([{"order_no": wo_a.name, "id": 1, "status": "IN_PROGRESS",
                              "updated_at": "2026-11-02T10:00:00+00:00"}])
            # B's instance names an order of A: it must not touch it.
            return page([
                {"order_no": wo_b.name, "id": 2, "status": "DONE", "produced_qty": 2,
                 "updated_at": "2026-11-03T10:00:00+00:00"},
                {"order_no": wo_a.name, "id": 99, "status": "CANCELLED",
                 "updated_at": "2026-11-03T11:00:00+00:00"},
            ])

        with self.route(answer) as calls:
            self.env["openmes.work.order"]._cron_openmes_pull_status()
        pulls = self._by_company(calls, "/production/completions")
        self.assertEqual((len(pulls["A"]), len(pulls["B"])), (1, 1))
        self.assertEqual((wo_a.mes_status, wo_a.mes_id, wo_a.state), ("IN_PROGRESS", 1, "sent"))
        self.assertEqual((wo_b.mes_status, wo_b.mes_id), ("DONE", 2))
        self.assertEqual(str(self.company_a.openmes_status_since), "2026-11-02 10:00:00")
        self.assertEqual(str(self.company_b.openmes_status_since), "2026-11-03 11:00:00")

    def test_lot_of_finished_product_in_the_mo_company(self):
        self._configure(self.company_b, order_source="manufacturing_orders")
        loaf_b = self.env["product.product"].create({
            "name": "Loaf B", "default_code": "LOAF-B", "openmes_manufacture": True, "is_storable": True,
            "tracking": "lot", "company_id": self.company_b.id,
        })
        production = self.env["mrp.production"].with_company(self.company_b).create({
            "product_id": loaf_b.id, "product_qty": 2, "company_id": self.company_b.id,
        })
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_status": "PENDING", "sent_date": "2026-11-01 08:00:00"})

        def answer(call):
            if call["company"] == "B" and call["path"].startswith("/production/completions"):
                return page([{"order_no": work_order.name, "id": 3, "status": "DONE", "produced_qty": 2,
                              "planned_qty": 2, "updated_at": "2026-11-03T10:00:00+00:00"}])
            return None

        with self.route(answer):
            self.env["openmes.work.order"]._cron_openmes_pull_status()
        self.assertEqual(production.state, "done", work_order.last_error)
        self.assertEqual(production.lot_producing_id.company_id, self.company_b)

    def test_change_requests_and_settlement_per_company(self):
        wo_a = self._sent(self.company_a, change_request_id=11, change_request_type="CHANGE",
                          change_request_status="SUBMITTED")
        wo_b = self._sent(self.company_b, change_request_id=11, change_request_type="CHANGE",
                          change_request_status="SUBMITTED")

        def answer(call):
            if call["path"].startswith("/change-requests"):
                if call["company"] == "A":
                    return FakeResponse(404, {"message": "Not Found"})
                return page([{"id": 11, "status": "APPLIED", "updated_at": "2026-11-03T10:00:00+00:00"}])
            return None

        with self.route(answer) as calls:
            self.env["openmes.work.order"]._cron_openmes_pull_status()
        self.assertEqual(len(self._by_company(calls, "/change-requests")["B"]), 1)
        self.assertEqual(wo_a.change_request_status, "SUBMITTED", "A's 404 affects only A")
        self.assertEqual(wo_b.change_request_status, "APPLIED", "same id in A is not touched by B")
        self.assertFalse(self.company_a.openmes_cr_since)

        (wo_a | wo_b).write({"mes_status": "DONE", "settlement_state": "pending"})
        settlement = FakeResponse(200, {"data": {"status": "DONE", "consumption": [], "scrap": {"total_qty": 0},
                                                 "costs": {"total_cost": 1, "currency": "EUR"}}})
        with self.route(lambda call: settlement) as calls:
            self.env["openmes.work.order"]._cron_openmes_fetch_settlements()
        self.assertEqual(sorted((c["company"], c["path"].split("?")[0]) for c in calls),
                         [("A", f"/work-orders/{wo_a.name}"), ("B", f"/work-orders/{wo_b.name}")])
        self.assertEqual((wo_a.settlement_state, wo_b.settlement_state), ("done", "done"))

    # ── Master data and stock ────────────────────────────────────────────────

    def test_master_data_per_company(self):
        self.flour.with_company(self.company_b).standard_price = 3.0
        with self.route() as calls:
            self.env["openmes.sync"]._cron_sync_master_data()
        products = self._by_company(calls, "/products/import")
        codes = {
            company: sorted(row["code"] for call in company_calls for row in call["json"]["products"])
            for company, company_calls in products.items()
        }
        self.assertIn("BAGEL-A", codes["A"])
        self.assertNotIn("BUN-B", codes["A"])
        self.assertIn("BUN-B", codes["B"])
        self.assertNotIn("BAGEL-A", codes["B"])
        self.assertIn("BREAD-01", codes["A"])
        self.assertIn("BREAD-01", codes["B"], "a shared product goes to both")

        materials = self._by_company(calls, "/materials/import")
        flour = {
            company: next(row for row in company_calls[0]["json"]["materials"] if row["code"] == "FLOUR-01")
            for company, company_calls in materials.items()
        }
        self.assertEqual((flour["A"]["unit_price"], flour["A"]["price_currency"]),
                         (2.5, self.company_a.currency_id.name))
        self.assertEqual((flour["B"]["unit_price"], flour["B"]["price_currency"]), (3.0, "EUR"))
        logs = self.env["openmes.sync.log"].search([("operation", "=", "products")], limit=2)
        self.assertEqual(set(logs.company_id.ids), {self.company_a.id, self.company_b.id})

    def test_workstation_codes_per_company(self):
        Workcenter = self.env["mrp.workcenter"]
        wc_a = Workcenter.create({"name": "Oven A", "company_id": self.company_a.id, "openmes_workstation_code": "WS-1"})
        wc_b = Workcenter.with_company(self.company_b).create(
            {"name": "Oven B", "company_id": self.company_b.id, "openmes_workstation_code": "WS-1"}
        )
        self.assertEqual(wc_b.openmes_workstation_code, "WS-1", "the same code in two OpenMES instances")
        with self.assertRaises(ValidationError):
            Workcenter.with_company(self.company_b).create(
                {"name": "Oven B2", "company_id": self.company_b.id, "openmes_workstation_code": "WS-1"}
            )
        for product, company, workcenter in ((self.bagel_a, self.company_a, wc_a), (self.bun_b, self.company_b, wc_b)):
            self.env["mrp.bom"].with_company(company).create({
                "product_tmpl_id": product.product_tmpl_id.id,
                "company_id": company.id,
                "bom_line_ids": [(0, 0, {"product_id": self.flour.id, "product_qty": 1})],
                "operation_ids": [(0, 0, {"name": "Baking", "workcenter_id": workcenter.id})],
            })

        with self.route() as calls:
            self.env["openmes.sync"]._cron_sync_master_data()
        routings = self._by_company(calls, "/process-templates/import")
        templates = {
            company: {t["product_type_code"]: t["steps"] for call in company_calls for t in call["json"]["templates"]}
            for company, company_calls in routings.items()
        }
        self.assertEqual(list(templates["A"]), ["BAGEL-A"])
        self.assertEqual(list(templates["B"]), ["BUN-B"])
        self.assertEqual((templates["A"]["BAGEL-A"][0]["workstation_code"], templates["A"]["BAGEL-A"][0]["line_code"]),
                         ("WS-1", "L1"))
        self.assertEqual((templates["B"]["BUN-B"][0]["workstation_code"], templates["B"]["BUN-B"][0]["line_code"]),
                         ("WS-1", self.line_b1.code))

    def test_shared_bom_sends_only_own_or_shared_work_centers(self):
        Workcenter = self.env["mrp.workcenter"]
        # Odoo refuses a company work center on a shared BoM, but a shared
        # work center moved into a company later stays on the BoMs using it.
        wc_a = Workcenter.create({"name": "Oven A", "company_id": False, "resource_calendar_id": False,
                                  "openmes_workstation_code": "WS-A"})
        wc_shared = Workcenter.create({"name": "Mixer", "company_id": False, "resource_calendar_id": False,
                                       "openmes_workstation_code": "WS-MIX"})
        bom = self.env["mrp.bom"].search([("product_tmpl_id", "=", self.bread.product_tmpl_id.id)])
        self.assertFalse(bom.company_id, "the bread BoM is shared")
        bom.write({"operation_ids": [
            (0, 0, {"name": "Mixing", "workcenter_id": wc_shared.id, "sequence": 1}),
            (0, 0, {"name": "Baking", "workcenter_id": wc_a.id, "sequence": 2}),
        ]})
        wc_a.company_id = self.company_a
        self.assertEqual(bom.operation_ids[1].workcenter_id.company_id, self.company_a)

        with self.route() as calls:
            self.env["openmes.sync"]._cron_sync_master_data()
        steps = {
            company: next(t["steps"] for call in company_calls for t in call["json"]["templates"]
                          if t["product_type_code"] == "BREAD-01")
            for company, company_calls in self._by_company(calls, "/process-templates/import").items()
        }
        self.assertEqual([s.get("workstation_code") for s in steps["A"]], ["WS-MIX", "WS-A"])
        # Company B's OpenMES does not get company A's workstation.
        self.assertEqual([s.get("workstation_code") for s in steps["B"]], ["WS-MIX", None])
        self.assertNotIn("line_code", steps["B"][1])
        self.assertEqual(steps["B"][0]["line_code"], self.line_b1.code)

    def test_stock_snapshot_per_company(self):
        with self.route() as calls:
            self.env["openmes.sync"]._cron_sync_stock()
        stock = self._by_company(calls, "/stock/import")
        self.assertEqual([c["json"]["warehouse_code"] for c in stock["A"]], ["WA"])
        self.assertEqual([c["json"]["warehouse_code"] for c in stock["B"]], ["WB"])
        documents = self._by_company(calls, "/stock-documents")
        self.assertEqual((len(documents["A"]), len(documents["B"])), (1, 1))

    def test_material_lots_per_company(self):
        Quant = self.env["stock.quant"]
        lot_a = self.env["stock.lot"].create({"name": "YA-1", "product_id": self.yeast.id, "company_id": self.company_a.id})
        lot_b = self.env["stock.lot"].create({"name": "YB-1", "product_id": self.yeast.id, "company_id": self.company_b.id})
        Quant._update_available_quantity(self.yeast, self.wh_a.lot_stock_id, 3, lot_id=lot_a)
        Quant.with_company(self.company_b)._update_available_quantity(
            self.yeast, self.wh_b.lot_stock_id, 5, lot_id=lot_b
        )
        with self.route() as calls:
            self.env["openmes.sync"]._cron_sync_stock()
        lots = self._by_company(calls, "/material-lots/import")
        self.assertEqual([c["json"]["warehouse_code"] for c in lots["A"]], ["WA"])
        self.assertEqual([c["json"]["warehouse_code"] for c in lots["B"]], ["WB"])
        self.assertEqual([r["lot_number"] for r in lots["A"][0]["json"]["lots"]], ["YA-1"])
        self.assertEqual([r["lot_number"] for r in lots["B"][0]["json"]["lots"]], ["YB-1"])
        Sent = self.env["openmes.material.lot.sent"]
        sent = Sent.search([])
        self.assertEqual(
            {(s.lot_number, s.company_id, s.warehouse_id) for s in sent},
            {("YA-1", self.company_a, self.wh_a), ("YB-1", self.company_b, self.wh_b)},
        )
        admin_a = Sent.with_user(self.env.ref("base.user_admin")).with_context(allowed_company_ids=self.company_a.ids)
        self.assertEqual(admin_a.search([]).mapped("lot_number"), ["YA-1"],
                         "the multi-company rule hides company B's lots")

        # Company A's lot runs out: only A's OpenMES gets a 0, B's lot is untouched.
        Quant._update_available_quantity(self.yeast, self.wh_a.lot_stock_id, -3, lot_id=lot_a)
        with self.route() as calls:
            self.env["openmes.sync"]._cron_sync_stock()
        lots = self._by_company(calls, "/material-lots/import")
        self.assertEqual(lots["A"][0]["json"]["lots"], [
            {"material_code": "YEAST-01", "lot_number": "YA-1", "quantity_available": 0.0},
        ])
        self.assertEqual(lots["B"][0]["json"]["lots"][0]["quantity_available"], 5)
        self.assertEqual(Sent.search([]).mapped("lot_number"), ["YB-1"])

    def test_stock_documents_per_company(self):
        def doc(mes_id, warehouse):
            return {"id": mes_id, "document_no": f"DOC-{warehouse}-{mes_id}", "type": "material_receipt",
                    "direction": "in", "warehouse_code": warehouse, "warehouse_erp_code": None,
                    "posted_at": "2026-11-03T18:37:13+00:00",
                    "lines": [{"material_code": "FLOUR-01", "quantity": 1, "unit_of_measure": "kg"}]}

        def answer(call):
            if call["path"].startswith("/stock-documents") and call["method"] == "GET":
                # The same id in both instances; A also names B's warehouse.
                rows = [doc(7, "WA"), doc(8, "WB")] if call["company"] == "A" else [doc(7, "WB")]
                return page(rows)
            return None

        with self.route(answer) as calls:
            self.env["openmes.stock.document"]._cron_openmes_stock_documents()
        Document = self.env["openmes.stock.document"]
        doc_a = Document.search([("mes_id", "=", 7), ("company_id", "=", self.company_a.id)])
        doc_b = Document.search([("mes_id", "=", 7), ("company_id", "=", self.company_b.id)])
        self.assertEqual((doc_a.state, doc_a.warehouse_id), ("acked", self.wh_a))
        self.assertEqual((doc_b.state, doc_b.warehouse_id), ("acked", self.wh_b))
        self.assertEqual(doc_b.move_ids.company_id, self.company_b)
        foreign = Document.search([("mes_id", "=", 8)])
        self.assertEqual((foreign.company_id, foreign.state), (self.company_a, "error"))
        self.assertIn("Unknown OpenMES warehouse", foreign.error_message)
        acks = sorted((c["company"], c["path"]) for c in calls if c["path"].endswith("/ack"))
        self.assertEqual(acks, [("A", "/stock-documents/7/ack"), ("B", "/stock-documents/7/ack")])

        # A's backlog is empty now: only A's unbooked document leaves it.
        doc_b2 = Document.create(
            {"name": "B-9", "mes_id": 9, "company_id": self.company_b.id, "warehouse_code": "WB"}
        )
        with self.route(lambda call: page() if call["company"] == "A" else None):
            self.env["openmes.stock.document"].with_company(self.company_a)._openmes_sync_documents()
        self.assertEqual(foreign.handling, "external")
        self.assertEqual(doc_b2.state, "pending", "B's documents are not touched by A's run")
        self.assertEqual(Document._openmes_blocking(self.wh_b), doc_b2)
        self.assertFalse(Document._openmes_blocking(self.wh_a))

    # ── Lines ────────────────────────────────────────────────────────────────

    def test_lines_per_company(self):
        def answer(call):
            rows = [{"code": "L1", "name": f"Line one {call['company']}", "is_active": True}]
            if call["company"] == "A":
                rows.append({"code": "L2", "name": "Line two", "is_active": True})
            else:
                rows.append({"code": "L7", "name": "B only", "is_active": True})
            return page(rows)

        with self.route(answer):
            self.env["openmes.line"]._cron_openmes_refresh_lines()
        Line = self.env["openmes.line"].with_context(active_test=False)
        self.assertEqual(self.line_l1.name, "Line one A")
        self.assertEqual(self.line_b1.name, "Line one B")
        line_b7 = Line.search([("code", "=", "L7")])
        self.assertEqual(line_b7.company_id, self.company_b)

        # B's list no longer has L7: deactivated in B only; A's L2 stays.
        with self.route(lambda call: page([{"code": "L1", "name": "Line one B"}])):
            self.env["openmes.line"].with_company(self.company_b)._openmes_refresh()
        self.assertFalse(line_b7.active)
        self.assertTrue(self.line_l2.active)
        self.assertTrue(self.line_l1.active)

    def test_shared_product_uses_the_line_of_the_order_company(self):
        # The cake has A's line L2; ordered in B it uses B's default line.
        wo_a = self._order(self.company_a, self.cake).openmes_work_order_ids
        wo_b = self._order(self.company_b, self.cake).openmes_work_order_ids
        self.assertEqual(wo_a.line_code, "L2")
        self.assertEqual(wo_b.line_code, "L1")
        with self.assertRaises(ValidationError):
            self.bun_b.product_tmpl_id.openmes_line_id = self.line_l2

    # ── Webhooks ─────────────────────────────────────────────────────────────

    def _deliver(self, secret, event, data, delivery="d-1"):
        raw = json.dumps({"event": event, "data": data}).encode()
        ts = str(int(time.time()))
        signature = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        headers = {
            "X-OpenMES-Event": event,
            "X-OpenMES-Delivery": delivery,
            "X-OpenMES-Timestamp": ts,
            "X-OpenMES-Signature": signature,
        }
        return self.env["openmes.webhook"]._openmes_receive(headers, raw)

    def _process(self):
        self.env["openmes.webhook.event"]._cron_openmes_process_events()
        self.env.invalidate_all()

    def test_webhook_per_company(self):
        from ..models.openmes_webhook import WebhookRejected

        self._configure(self.company_a, webhook_id=1, webhook_secret=SECRET_A)
        self._configure(self.company_b, webhook_id=2, webhook_secret=SECRET_B)
        wo_a = self._sent(self.company_a)
        wo_b = self._sent(self.company_b)

        def row(work_order, status="IN_PROGRESS"):
            return {"order_no": work_order.name, "status": status, "updated_at": "2026-11-06T09:15:01+00:00"}

        event = "work_order.status_changed"
        self.assertEqual(self._deliver(SECRET_B, event, row(wo_b)), (200, {"status": "queued"}))
        self._process()
        self.assertEqual((wo_a.mes_status, wo_b.mes_status), ("PENDING", "IN_PROGRESS"))
        # The same delivery id from A's instance is another delivery.
        self.assertEqual(self._deliver(SECRET_A, event, row(wo_a)), (200, {"status": "queued"}))
        self._process()
        self.assertEqual(wo_a.mes_status, "IN_PROGRESS")
        events = self.env["openmes.webhook.event"].search([("delivery_id", "=", "d-1")])
        self.assertEqual(set(events.company_id.ids), {self.company_a.id, self.company_b.id})
        self.assertEqual(set(events.mapped("result")), {"processed"})
        # A repeat of A's delivery is acknowledged and changes nothing.
        self.assertEqual(self._deliver(SECRET_A, event, row(wo_a, "DONE")), (200, {"status": "duplicate"}))
        self._process()
        self.assertEqual(wo_a.mes_status, "IN_PROGRESS")
        # A's instance naming B's order changes nothing.
        self.assertEqual(self._deliver(SECRET_A, event, row(wo_b, "DONE"), "d-2"), (200, {"status": "queued"}))
        self._process()
        self.assertEqual(wo_b.mes_status, "IN_PROGRESS")
        foreign = self.env["openmes.webhook.event"].search([("delivery_id", "=", "d-2")])
        self.assertEqual((foreign.company_id, foreign.state, foreign.result), (self.company_a, "done", "ignored"))
        with self.assertRaises(WebhookRejected):
            self._deliver("Cc3" + "c" * 37, event, row(wo_b, "DONE"), "d-3")
        self._configure(self.company_b, enabled=False)
        self.assertEqual(self._deliver(SECRET_B, event, row(wo_b, "DONE"), "d-4"), (200, {"status": "ignored"}))
        self._process()
        self.assertEqual(wo_b.mes_status, "IN_PROGRESS")

    def test_webhook_events_per_company_visibility(self):
        """A manager of company A does not see the events of company B."""
        self._configure(self.company_a, webhook_id=1, webhook_secret=SECRET_A)
        self._configure(self.company_b, webhook_id=2, webhook_secret=SECRET_B)
        wo_a = self._sent(self.company_a)
        wo_b = self._sent(self.company_b)
        data = {"status": "IN_PROGRESS", "updated_at": "2026-11-06T09:15:01+00:00"}
        self._deliver(SECRET_A, "work_order.status_changed", dict(data, order_no=wo_a.name), "a-1")
        self._deliver(SECRET_B, "work_order.status_changed", dict(data, order_no=wo_b.name), "b-1")
        manager = new_test_user(
            self.env, "om_mc_event_manager", groups="sales_team.group_sale_manager",
            company_id=self.company_a.id, company_ids=[(6, 0, self.company_a.ids)],
        )
        seen = self.env["openmes.webhook.event"].with_user(manager).search([])
        self.assertEqual(seen.mapped("delivery_id"), ["a-1"])

    def test_webhook_event_runs_in_the_signing_company(self):
        """B in MO mode, the superuser's current company is A: a DONE signed
        with B's secret closes B's MO, and the backorder and its work order
        belong to B."""
        self._configure(self.company_b, webhook_id=2, webhook_secret=SECRET_B, order_source="manufacturing_orders")
        self.env["mrp.bom"].create({
            "product_tmpl_id": self.bun_b.product_tmpl_id.id, "product_qty": 10, "company_id": self.company_b.id,
            "bom_line_ids": [(0, 0, {"product_id": self.flour.id, "product_qty": 1})],
        })
        production = self.env["mrp.production"].with_company(self.company_b).create({
            "product_id": self.bun_b.id, "product_qty": 10, "company_id": self.company_b.id,
        })
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 5, "mes_status": "IN_PROGRESS"})
        self.assertEqual(self.env.company, self.company_a)

        WorkOrder = type(self.env["openmes.work.order"])
        apply_status = WorkOrder._openmes_apply_status
        seen = []

        def spy(records, row):
            seen.append(records.env.company)
            return apply_status(records, row)

        data = {"order_no": work_order.name, "id": 5, "status": "DONE", "produced_qty": 6, "planned_qty": 10,
                "updated_at": "2026-11-06T09:15:01+00:00"}
        with patch.object(WorkOrder, "_openmes_apply_status", spy):
            result = self._deliver(SECRET_B, "work_order.status_changed", data)
            self.assertEqual(result, (200, {"status": "queued"}))
            self.assertFalse(seen, "nothing is applied in the request")
            self._process()
        self.assertEqual(seen, [self.company_b])
        self.assertEqual(production.state, "done")
        backorder = production.procurement_group_id.mrp_production_ids - production
        self.assertEqual(backorder.company_id, self.company_b)
        self.assertEqual(backorder.openmes_work_order_ids.company_id, self.company_b)
        self.assertEqual(backorder.openmes_work_order_ids.name[:4], "BBB-")

    # ── Changing the connection ──────────────────────────────────────────────

    def _connected(self):
        """Both companies pushed to by a webhook, with pull markers."""
        cron = self.env.ref("openmes_connector.ir_cron_openmes_pull_status")
        for company, webhook_id, secret in ((self.company_a, 1, SECRET_A), (self.company_b, 2, SECRET_B)):
            self._configure(
                company, webhook_id=webhook_id, webhook_secret=secret,
                webhook_url="https://odoo.example.test/openmes/webhook",
                status_since="2026-11-01 08:00:00", cr_since="2026-11-01 09:00:00",
            )
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "hours"))
        return cron

    def _connection_state(self, company):
        return (company.openmes_status_since, company.openmes_cr_since, company.openmes_webhook_id,
                company.openmes_webhook_secret, company.openmes_webhook_url)

    def test_new_url_or_key_forgets_the_former_instance(self):
        for vals in ({"openmes_api_url": "https://mes-new.example.test"}, {"openmes_api_key": "omk_" + "n" * 40}):
            with self.subTest(vals=vals):
                cron = self._connected()
                before_b = self._connection_state(self.company_b)
                with self.assertLogs("odoo.addons.openmes_connector.models.res_company", "WARNING") as logs:
                    self.company_a.write(vals)
                self.assertEqual(self._connection_state(self.company_a), (False, False, 0, False, False))
                self.assertEqual((cron.interval_number, cron.interval_type), (15, "minutes"))
                self.assertEqual(self._connection_state(self.company_b), before_b, "B is untouched")
                output = "\n".join(logs.output)
                self.assertIn("webhook 1", output)
                for secret in (SECRET_A, API_KEY, "omk_" + "n" * 40):
                    self.assertNotIn(secret, output)
                self.company_a.write({"openmes_api_url": API_URL, "openmes_api_key": API_KEY})

    def test_same_connection_keeps_markers_and_webhook(self):
        self._connected()
        before = self._connection_state(self.company_a)
        self.company_a.write({"openmes_api_url": API_URL + "/ ", "openmes_api_key": " " + API_KEY})
        self.company_a.write({"openmes_order_prefix": "NEW"})
        self.env["res.config.settings"].create({"openmes_order_prefix": "NEW2"}).execute()
        self.assertEqual(self._connection_state(self.company_a), before)

    def test_new_url_through_settings_forgets_the_webhook(self):
        self._connected()
        with mute_logger("odoo.addons.openmes_connector.models.res_company"):
            self.env["res.config.settings"].create({"openmes_api_url": "https://mes-new.example.test"}).execute()
        self.assertEqual(self.company_a.openmes_api_url, "https://mes-new.example.test")
        self.assertEqual(self._connection_state(self.company_a), (False, False, 0, False, False))

    def test_line_refresh_logs_belong_to_the_company(self):
        def answer(call):
            if call["company"] == "B":
                return FakeResponse(401, {"message": "Unauthenticated."})
            return page([{"code": "L1", "name": "Line one"}])

        with self.route(answer), mute_logger("odoo.addons.openmes_connector.models.openmes_line"):
            self.env["openmes.line"]._cron_openmes_refresh_lines()
        logs = self.env["openmes.sync.log"].search([("operation", "=", "lines")], order="id desc", limit=2)
        self.assertEqual(
            {(log.company_id, log.state) for log in logs},
            {(self.company_a, "success"), (self.company_b, "failed")},
        )

    def test_warehouse_code_is_unique_per_company(self):
        # The same code in another company is another OpenMES instance.
        self.wh_b.openmes_code = "WA"
        with self.assertRaises(ValidationError):
            self.env["stock.warehouse"].create({
                "name": "Second A", "code": "WA2", "company_id": self.company_a.id, "openmes_code": "WA",
            })
        manager = new_test_user(self.env, "om_stock_manager_wh", groups="stock.group_stock_manager",
                                company_id=self.company_a.id)
        second = self.env["stock.warehouse"].create({"name": "Second A", "code": "WA2",
                                                     "company_id": self.company_a.id})
        with self.assertRaises(ValidationError):
            second.with_user(manager).openmes_code = "WA"
        second.with_user(manager).openmes_code = "WA-2"
        self.assertEqual(second.openmes_code, "WA-2")

    def test_webhook_registration_per_company(self):
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://odoo.example.test")
        cron = self.env.ref("openmes_connector.ir_cron_openmes_pull_status")
        cron.write({"interval_number": 15, "interval_type": "minutes"})

        def register(company, webhook_id, secret):
            answer = FakeResponse(201, {"data": {"id": webhook_id}, "secret": secret})
            with self.route(lambda call: answer) as calls:
                self.env["res.config.settings"].with_company(company).create({}).action_openmes_register_webhook()
            return calls

        calls = register(self.company_b, 21, SECRET_B)
        self.assertEqual(calls[0]["company"], "B")
        self.assertIn("Plant B", calls[0]["json"]["name"])
        self.assertEqual((self.company_b.openmes_webhook_id, self.company_b.openmes_webhook_secret), (21, SECRET_B))
        self.assertFalse(self.company_a.openmes_webhook_secret)
        self.assertEqual((cron.interval_number, cron.interval_type), (15, "minutes"), "A still polls")
        register(self.company_a, 11, SECRET_A)
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "hours"))

        with self.route(lambda call: FakeResponse(204, None)) as calls:
            self.env["res.config.settings"].with_company(self.company_b).create({}) \
                .action_openmes_unregister_webhook()
        self.assertEqual([(c["company"], c["path"]) for c in calls], [("B", "/webhooks/21")])
        self.assertFalse(self.company_b.openmes_webhook_secret)
        self.assertEqual(self.company_a.openmes_webhook_secret, SECRET_A)
        self.assertEqual((cron.interval_number, cron.interval_type), (15, "minutes"))

    # ── Settings ─────────────────────────────────────────────────────────────

    def test_settings_write_the_current_company(self):
        settings = self.env["res.config.settings"].with_company(self.company_b).create({
            "openmes_api_url": "https://mes-b2.example.test/",
            "openmes_order_prefix": "PLB",
        })
        settings.execute()
        self.assertEqual(self.company_b.openmes_api_url, "https://mes-b2.example.test")
        self.assertEqual(self.company_b.openmes_order_prefix, "PLB")
        self.assertEqual((self.company_a.openmes_api_url, self.company_a.openmes_order_prefix), (API_URL, "ODOO"))
        self.assertEqual(self.env["res.config.settings"].with_company(self.company_b).create({})
                         .openmes_default_line_id, self.line_b1)

    def test_constraints_on_the_company(self):
        invalid = [
            {"openmes_api_key": False},
            {"openmes_api_url": "ftp://mes.example.test"},
            {"openmes_api_key": "nope"},
            {"openmes_order_prefix": "B/B"},
            # The same URL and key as company A: both would book the same documents.
            {"openmes_api_url": API_URL, "openmes_api_key": API_KEY},
        ]
        for vals in invalid:
            with self.subTest(vals=vals), self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.company_b.write(vals)
        # The same instance with another key is allowed.
        self.company_b.write({"openmes_api_url": API_URL})
        # A disabled company may keep a copy of A's settings.
        self.company_b.write({"openmes_enabled": False, "openmes_api_key": API_KEY})

    # ── Access ───────────────────────────────────────────────────────────────

    def test_users_see_only_their_company(self):
        wo_a = self._work_order(self.company_a)
        wo_b = self._work_order(self.company_b)
        log_b = self.env["openmes.sync.log"].create(
            {"operation": "products", "state": "success", "company_id": self.company_b.id}
        )
        doc_b = self.env["openmes.stock.document"].create(
            {"name": "B-1", "mes_id": 1, "company_id": self.company_b.id}
        )
        only_a = {"company_id": self.company_a.id, "company_ids": [(6, 0, self.company_a.ids)]}
        users = {
            "salesman": new_test_user(self.env, "mc_salesman", groups="sales_team.group_sale_salesman_all_leads",
                                      **only_a),
            "manager": new_test_user(self.env, "mc_manager", groups="sales_team.group_sale_manager", **only_a),
        }
        for name, user in users.items():
            with self.subTest(user=name):
                WorkOrder = self.env["openmes.work.order"].with_user(user)
                found = WorkOrder.search([("id", "in", (wo_a | wo_b).ids)])
                self.assertEqual(found, wo_a)
                with self.assertRaises(AccessError):
                    wo_b.with_user(user).read(["name"])
        admin_a = new_test_user(self.env, "mc_admin_a", groups="base.group_system,stock.group_stock_user", **only_a)
        self.assertFalse(self.env["openmes.sync.log"].with_user(admin_a).search([("id", "=", log_b.id)]))
        self.assertFalse(self.env["openmes.stock.document"].with_user(admin_a).search([("id", "=", doc_b.id)]))
        with self.assertRaises(AccessError):
            doc_b.with_user(admin_a).read(["name"])

    def test_secrets_are_admin_only(self):
        self._configure(webhook_secret=SECRET_A)
        users = [
            new_test_user(self.env, "mc_sale_manager", groups="sales_team.group_sale_manager"),
            new_test_user(self.env, "mc_erp_manager", groups="base.group_erp_manager"),
        ]
        for user in users:
            company = self.company_a.with_user(user)
            for field in ("openmes_api_key", "openmes_webhook_secret", "openmes_api_url"):
                with self.subTest(user=user.login, field=field), self.assertRaises(AccessError):
                    company.read([field])
            settings = self.env["res.config.settings"].create({}).with_user(user)
            for method in ("action_openmes_test_connection", "action_openmes_sync_master_data",
                           "action_openmes_register_webhook"):
                with self.subTest(user=user.login, method=method), self.route(), self.assertRaises(AccessError):
                    getattr(settings, method)()
            # Nor can they switch the integration or change what is sent.
            for vals in ({"openmes_enabled": False}, {"openmes_order_source": "manufacturing_orders"},
                         {"openmes_order_prefix": "X"}, {"openmes_default_line_id": False}):
                with self.subTest(user=user.login, vals=vals), self.assertRaises(AccessError):
                    company.write(vals)
        self.assertTrue(self.company_a.openmes_enabled)
        self.assertEqual(self.company_a.openmes_default_line_id, self.line_l1)

    # ── Settings saved in one go ─────────────────────────────────────────────

    def test_first_save_enables_a_fresh_company(self):
        company_c = self.env["res.company"].create({"name": "Plant C"})
        self.env.user.company_ids |= company_c
        Settings = self.env["res.config.settings"].with_company(company_c)
        Settings.create({
            "openmes_enabled": True,
            "openmes_api_url": "https://mes-c.example.test/",
            "openmes_api_key": "omk_" + "c" * 40,
            "openmes_order_prefix": "CCC",
        }).execute()
        self.assertTrue(company_c.openmes_enabled)
        self.assertEqual(company_c.openmes_api_url, "https://mes-c.example.test")
        self.assertEqual(company_c.openmes_order_prefix, "CCC")
        # The reverse in one save: disable and clear the key.
        Settings.create({"openmes_enabled": False, "openmes_api_key": False}).execute()
        self.assertFalse(company_c.openmes_enabled)
        self.assertFalse(company_c.openmes_api_key)
        # Still refused when the key is missing.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            Settings.create({"openmes_enabled": True})
        self.assertFalse(company_c.openmes_enabled)

    def test_pull_interval_follows_company_changes(self):
        cron = self.env.ref("openmes_connector.ir_cron_openmes_pull_status")
        self._configure(self.company_a, webhook_id=1, webhook_secret=SECRET_A)
        self.assertEqual((cron.interval_number, cron.interval_type), (15, "minutes"), "B has no webhook")
        # Changed outside Settings (company form, RPC).
        self.company_b.write({"openmes_enabled": False})
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "hours"))
        self.company_b.write({"openmes_enabled": True})
        self.assertEqual((cron.interval_number, cron.interval_type), (15, "minutes"))

    # ── One company's failure does not stop the others ───────────────────────

    def test_cron_isolates_unexpected_errors(self):
        wo_a = self._sent(self.company_a)
        wo_b = self._sent(self.company_b)

        def answer(call):
            if not call["path"].startswith("/production/completions"):
                return None
            if call["company"] == "A":
                return page([{"order_no": wo_a.name, "id": 1, "status": "IN_PROGRESS",
                              "updated_at": "2026-11-02T10:00:00+00:00"}])
            # Not an API error: a value that breaks float().
            return page([{"order_no": wo_b.name, "id": 2, "status": "DONE", "produced_qty": "lots",
                          "updated_at": "2026-11-03T10:00:00+00:00"}])

        with self.route(answer), \
                self.assertLogs("odoo.addons.openmes_connector.models.res_company", "ERROR") as logs:
            self.env["openmes.work.order"]._cron_openmes_pull_status()
        self.assertIn(f"status pull failed for company {self.company_b.id}", logs.output[0])
        self.assertEqual((wo_a.mes_status, wo_a.mes_id), ("IN_PROGRESS", 1))
        self.assertEqual(str(self.company_a.openmes_status_since), "2026-11-02 10:00:00")
        self.assertEqual((wo_b.mes_status, wo_b.mes_id), ("PENDING", 0), "B's partial run is rolled back")
        self.assertFalse(self.company_b.openmes_status_since)

    # ── Crons with nothing enabled ───────────────────────────────────────────

    def test_crons_do_nothing_without_an_enabled_company(self):
        self._work_order(self.company_a)
        self._configure(self.company_a, enabled=False)
        self._configure(self.company_b, enabled=False)
        with self.route() as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
            self.env["openmes.work.order"]._cron_openmes_pull_status()
            self.env["openmes.work.order"]._cron_openmes_fetch_settlements()
            self.env["openmes.sync"]._cron_sync_master_data()
            self.env["openmes.sync"]._cron_sync_stock()
            self.env["openmes.stock.document"]._cron_openmes_stock_documents()
            self.env["openmes.line"]._cron_openmes_refresh_lines()
        self.assertFalse(calls)


@tagged("post_install", "-at_install")
class TestLegacySettingsMigration(OpenmesCase):
    PARAMS = {
        "openmes_connector.enabled": "True",
        "openmes_connector.api_url": "https://legacy.example.test/",
        "openmes_connector.api_key": "omk_" + "l" * 40,
        "openmes_connector.default_line_code": "LEG",
        "openmes_connector.order_prefix": "OLD",
        "openmes_connector.order_source": "manufacturing_orders",
        "openmes_connector.status_since": "2026-10-01T08:00:00Z",
        "openmes_connector.change_request_since": "2026-10-02T09:00:00Z",
        "openmes_connector.webhook_id": "33",
        "openmes_connector.webhook_secret": SECRET_A,
        "openmes_connector.webhook_url": "https://odoo.example.test/openmes/webhook",
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.main = cls.env.ref("base.main_company")
        cls.icp = cls.env["ir.config_parameter"].sudo()

    def _unconfigure(self):
        self.main.write({
            "openmes_enabled": False, "openmes_api_url": False, "openmes_api_key": False,
            "openmes_default_line_id": False,
        })

    def _set_params(self, **overrides):
        for key, value in dict(self.PARAMS, **overrides).items():
            self.icp.set_param(key, value)

    def _params_left(self):
        return [key for key in self.PARAMS if self.icp.get_param(key)]

    def test_legacy_params_move_to_the_main_company(self):
        self._unconfigure()
        self._set_params()
        Company = self.env["res.company"]
        self.assertTrue(Company._openmes_adopt_legacy_params())
        main = self.main
        self.assertTrue(main.openmes_enabled)
        self.assertEqual(main.openmes_api_url, "https://legacy.example.test")
        self.assertEqual(main.openmes_api_key, "omk_" + "l" * 40)
        self.assertEqual((main.openmes_order_prefix, main.openmes_order_source), ("OLD", "manufacturing_orders"))
        self.assertEqual((main.openmes_webhook_id, main.openmes_webhook_secret), (33, SECRET_A))
        self.assertEqual(str(main.openmes_status_since), "2026-10-01 08:00:00")
        self.assertEqual(str(main.openmes_cr_since), "2026-10-02 09:00:00")
        # No line record for the code yet: one is created in the main company.
        self.assertEqual(main.openmes_default_line_id.code, "LEG")
        self.assertEqual(main.openmes_default_line_id.company_id, main)
        self.assertEqual(self._params_left(), [])

        before = main.read(["openmes_api_url", "openmes_default_line_id", "openmes_status_since"])
        self.assertFalse(Company._openmes_adopt_legacy_params(), "a second run finds nothing")
        self.assertEqual(main.read(["openmes_api_url", "openmes_default_line_id", "openmes_status_since"]), before)

    def test_existing_line_is_reused(self):
        self._unconfigure()
        self._set_params(**{"openmes_connector.default_line_code": "L2"})
        self.env["res.company"]._openmes_adopt_legacy_params()
        self.assertEqual(self.main.openmes_default_line_id, self.line_l2)

    def test_configured_company_is_not_overwritten(self):
        self._set_params()
        self.env["res.company"]._openmes_adopt_legacy_params()
        self.assertEqual(self.main.openmes_api_url, API_URL)
        self.assertEqual(self.main.openmes_api_key, API_KEY)
        self.assertEqual(self.main.openmes_default_line_id, self.line_l1)
        self.assertEqual(self._params_left(), [], "the parameters are removed anyway")

    def test_dropped_legacy_webhook_is_reported(self):
        self._set_params()
        with self.assertLogs("odoo.addons.openmes_connector.models.res_company", "WARNING") as logs:
            self.env["res.company"]._openmes_adopt_legacy_params()
        self.assertTrue(any("webhook 33" in line for line in logs.output))
        self.assertFalse(any(SECRET_A in line for line in logs.output))

    def test_invalid_legacy_values_are_imported_disabled(self):
        self._unconfigure()
        self._set_params(**{"openmes_connector.api_url": "mes.example.test"})
        with mute_logger("odoo.addons.openmes_connector.models.res_company"):
            self.env["res.company"]._openmes_adopt_legacy_params()
        self.assertFalse(self.main.openmes_enabled)
        self.assertEqual(self.main.openmes_api_url, "mes.example.test", "kept so it can be corrected")
        self.assertEqual(self.main.openmes_api_key, "omk_" + "l" * 40)
        self.assertEqual(self.main.openmes_webhook_secret, SECRET_A)
        self.assertEqual(self._params_left(), [])

    def test_key_is_never_logged(self):
        self._unconfigure()
        self._set_params(**{"openmes_connector.api_url": "mes.example.test"})
        with self.assertLogs("odoo.addons.openmes_connector.models.res_company", "WARNING") as logs:
            self.env["res.company"]._openmes_adopt_legacy_params()
        self.assertFalse(any("l" * 40 in line for line in logs.output))
