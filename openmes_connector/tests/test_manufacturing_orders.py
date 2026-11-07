from unittest.mock import patch

import requests

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from odoo.tools import float_round

from .common import FakeResponse, OpenmesCase, import_report


@tagged("post_install", "-at_install")
class TestManufacturingOrders(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._configure(order_source="manufacturing_orders")
        # Flour only: an MO of buns closes without lots.
        cls.bun = cls.env["product.product"].create({
            "name": "Bun", "default_code": "BUN-01", "is_storable": True, "openmes_manufacture": True,
        })
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.bun.product_tmpl_id.id,
            "product_qty": 10,
            "bom_line_ids": [(0, 0, {"product_id": cls.flour.id, "product_qty": 1})],
        })
        cls.plain = cls.env["product.product"].create({"name": "Plain roll", "default_code": "ROLL-01", "is_storable": True})
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.plain.product_tmpl_id.id,
            "product_qty": 1,
            "bom_line_ids": [(0, 0, {"product_id": cls.flour.id, "product_qty": 1})],
        })

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _set_source(self, mode):
        self._configure(order_source=mode)

    def _create_mo(self, product, qty, uom=None, confirm=True, **vals):
        production = self.env["mrp.production"].create({
            "product_id": product.id,
            "product_qty": qty,
            "product_uom_id": (uom or product.uom_id).id,
            **vals,
        })
        if confirm:
            production.action_confirm()
        return production

    def _sent(self, work_order, **vals):
        work_order.write(dict({"state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS",
                               "sent_date": "2026-11-01 08:00:00"}, **vals))

    @staticmethod
    def _page(rows):
        return FakeResponse(200, {"data": rows, "meta": {"next_cursor": None}})

    def _pull(self, *rows):
        with self.mock_openmes(self._page(list(rows))):
            self.env["openmes.work.order"]._cron_openmes_pull_status()

    @staticmethod
    def _done_row(work_order, produced, **extra):
        return dict({"order_no": work_order.name, "id": work_order.mes_id, "status": "DONE",
                     "produced_qty": produced, "planned_qty": work_order.planned_qty,
                     "completed_at": "2026-11-02T11:30:00+00:00", "updated_at": "2026-11-02T11:30:00+00:00"}, **extra)

    def _mto_order(self, product, qty, **order_vals):
        mto = self.env.ref("stock.route_warehouse0_mto")
        mto.active = True
        product.route_ids = [(6, 0, [mto.id, self.env.ref("mrp.route_warehouse0_manufacture").id])]
        order = self._create_order([(product, qty, {})])
        order.write(order_vals)
        order.action_confirm()
        production = self.env["mrp.production"].search([("product_id", "=", product.id)], order="id desc", limit=1)
        self.assertTrue(production, "the MTO route creates a manufacturing order")
        return order, production

    def _messages(self, record):
        return " ".join(m.body or "" for m in record.message_ids)

    # ── Settings ─────────────────────────────────────────────────────────────

    def test_unknown_source_falls_back_to_sale_lines(self):
        client = self.env["openmes.client"]
        self.assertEqual(client._order_source(), "manufacturing_orders")
        with self.assertRaises(ValueError):
            self._set_source("something_else")
        self._set_source(False)
        self.assertEqual(client._order_source(), "sale_lines")

    # ── Which source creates work orders ─────────────────────────────────────

    def test_chosen_lines_reach_the_payload(self):
        self.bun.product_tmpl_id.openmes_line_id = self.line_l2
        work_order = self._create_mo(self.bun, 1).openmes_work_order_ids
        self.assertEqual(work_order.line_code, "L2")
        with self.mock_openmes(import_report(imported=1)) as calls:
            work_order._openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["line_code"], "L2")

        self.bun.product_tmpl_id.openmes_line_id = False
        self.env["res.config.settings"].create({"openmes_default_line_id": self.line_l2.id}).execute()
        work_order = self._create_mo(self.bun, 1).openmes_work_order_ids
        with self.mock_openmes(import_report(imported=1)) as calls:
            work_order._openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["line_code"], "L2", "the default line from Settings")

    def test_confirm_mo_creates_one_work_order(self):
        production = self._create_mo(self.bun, 2, uom=self.uom_dozen)
        work_order = production.openmes_work_order_ids
        self.assertEqual(len(work_order), 1)
        self.assertEqual(work_order.name, "ODOO-" + production.name.replace("/", "-"))
        self.assertNotIn("/", work_order.name)
        self.assertEqual(work_order.source_type, "production")
        self.assertEqual(work_order.production_id, production)
        self.assertEqual(work_order.source_name, production.name)
        self.assertAlmostEqual(work_order.planned_qty, 24.0, msg="converted to the product UoM")
        self.assertEqual(work_order.line_code, "L1")
        self.assertEqual(work_order.company_id, production.company_id)
        self.assertEqual(work_order.state, "to_send")
        self.assertFalse(work_order.sale_order_id)
        self.assertFalse(work_order.partner_id)
        self.assertEqual(production.openmes_work_order_count, 1)
        action = production.action_view_openmes_work_orders()
        self.assertEqual(action["domain"], [("production_id", "=", production.id)])

    def test_sale_lines_not_used_in_mo_mode(self):
        order = self._create_order([(self.bread, 5, {}), (self.cake, 1, {})])
        order.action_confirm()
        self.assertFalse(order.openmes_work_order_ids)
        self.assertFalse(self.env["openmes.work.order"].search([("sale_order_id", "=", order.id)]))

    def test_mo_not_used_in_sale_lines_mode(self):
        self._set_source("sale_lines")
        production = self._create_mo(self.bun, 10)
        self.assertFalse(production.openmes_work_order_ids)
        order = self._create_order([(self.bread, 5, {})])
        order.action_confirm()
        self.assertEqual(len(order.openmes_work_order_ids), 1, "sale-line mode works as before")

    def test_mto_sale_order_gives_one_work_order_linked_to_the_mo(self):
        order, production = self._mto_order(self.bun, 7, client_order_ref="PO-9")
        work_orders = self.env["openmes.work.order"].search(
            ["|", ("sale_order_id", "=", order.id), ("origin_sale_order_id", "=", order.id)]
        )
        self.assertEqual(len(work_orders), 1, "no duplicate from the sale line")
        self.assertEqual(work_orders.production_id, production)
        self.assertEqual(work_orders.origin_sale_order_id, order)
        self.assertEqual(work_orders.partner_id, self.partner)
        self.assertEqual(order.openmes_work_order_count, 1)
        self.assertEqual(order.action_view_openmes_work_orders()["res_model"], "openmes.work.order")
        self.assertEqual(self.env["openmes.work.order"].search(order.action_view_openmes_work_orders()["domain"]),
                         work_orders)
        payload = work_orders._openmes_prepare_payload()
        self.assertEqual(payload["customer_order_no"], "PO-9")
        self.assertEqual(payload["customer_name"], "Bakery Customer")
        self.assertEqual(payload["extra_data"], {"manufacturing_order": production.name, "sale_order": order.name})
        self.assertIn("Bun", payload["description"])
        self.assertFalse(order.openmes_work_order_ids, "sale-side hooks only follow sale-line work orders")

    def test_nothing_created_for_unflagged_disabled_or_draft(self):
        self.assertFalse(self._create_mo(self.plain, 3).openmes_work_order_ids)
        draft = self._create_mo(self.bun, 3, confirm=False)
        draft._openmes_create_work_orders()
        self.assertFalse(draft.openmes_work_order_ids)
        self._configure(enabled=False)
        self.assertFalse(self._create_mo(self.bun, 3).openmes_work_order_ids)

    def test_creation_is_idempotent(self):
        production = self._create_mo(self.bun, 3)
        production._openmes_create_work_orders()
        production._openmes_create_work_orders()
        self.assertEqual(len(production.openmes_work_order_ids), 1)

    def test_long_and_colliding_names(self):
        long_mo = self._create_mo(self.bun, 1, name="WH/" + "X" * 120)
        self.assertEqual(len(long_mo.openmes_work_order_ids.name), 100)
        first = self._create_mo(self.bun, 1, name="A/1")
        second = self._create_mo(self.bun, 1, name="A-1")
        self.assertEqual(first.openmes_work_order_ids.name, "ODOO-A-1")
        self.assertEqual(second.openmes_work_order_ids.name, f"ODOO-A-1-{second.id}")

    def test_mode_switch_keeps_existing_sale_line_work_orders(self):
        self._set_source("sale_lines")
        order, production = self._mto_order(self.bun, 5)
        line_wo = order.openmes_work_order_ids
        self.assertEqual(len(line_wo), 1)
        self.assertFalse(production.openmes_work_order_ids)

        self._set_source("manufacturing_orders")
        production._openmes_create_work_orders()
        self.assertFalse(production.openmes_work_order_ids, "the origin line is already in OpenMES")
        self.assertIn("already has one", self._messages(production))

        self._sent(line_wo, mes_status="PENDING")
        order.order_line.product_uom_qty = 8
        self.assertEqual((line_wo.state, line_wo.planned_qty), ("to_send", 8))
        order._action_cancel()
        self.assertTrue(line_wo.pending_cancel)

    def test_source_constraint(self):
        production = self._create_mo(self.bun, 1)
        WorkOrder = self.env["openmes.work.order"]
        order = self._create_order([(self.bread, 1, {})])
        with self.assertRaises(ValidationError):
            WorkOrder.create({"name": "X-1", "product_id": self.bun.id, "source_type": "production"})
        with self.assertRaises(ValidationError):
            WorkOrder.create({"name": "X-2", "product_id": self.bun.id, "source_type": "production",
                              "production_id": production.id, "sale_order_id": order.id})
        with self.assertRaises(ValidationError):
            WorkOrder.create({"name": "X-3", "product_id": self.bun.id, "production_id": production.id})

    # ── Payload and OpenMES errors ───────────────────────────────────────────

    def test_send_payload_without_sale_order(self):
        production = self._create_mo(self.bun, 10, date_deadline="2026-12-24 10:00:00")
        work_order = production.openmes_work_order_ids
        result = [{"row": 1, "order_no": work_order.name, "id": 601, "action": "created"}]
        with self.mock_openmes(import_report(imported=1, results=result)) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        order = calls[0]["json"]["orders"][0]
        self.assertEqual(order["order_no"], work_order.name)
        self.assertEqual(order["product_type_code"], "BUN-01")
        self.assertEqual(order["planned_qty"], 10.0)
        self.assertEqual(order["external_ref"], f"mrp.production,{production.id}")
        self.assertEqual(order["due_date"], "2026-12-24")
        self.assertEqual(order["extra_data"], {"manufacturing_order": production.name})
        for key in ("unit_price", "price_currency", "customer_order_no", "customer_name"):
            self.assertNotIn(key, order)
        self.assertIn(production.name, order["change_reason"])
        self.assertEqual((work_order.state, work_order.mes_id), ("sent", 601))

    def test_due_date_falls_back_to_planned_end(self):
        production = self._create_mo(self.bun, 10)
        production.date_deadline = False
        self.assertEqual(production._openmes_due_date(), production.date_finished.date())

    def test_send_errors(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        cases = [
            import_report(errors=[{"row": 1, "field": "line_code", "message": "Line 'L1' not found"}]),
            FakeResponse(401, {"message": "Invalid"}),
        ]
        for response in cases:
            work_order.write({"state": "to_send", "attempt_count": 0})
            with self.mock_openmes(response):
                work_order._openmes_send()
            self.assertEqual(work_order.state, "error")

        def boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        work_order.write({"state": "to_send", "attempt_count": 0})
        with self.mock_openmes(boom):
            work_order._openmes_send()
        self.assertEqual(work_order.state, "error")
        self.assertIn("Cannot reach OpenMES", work_order.last_error)

    # ── Changes ──────────────────────────────────────────────────────────────

    def test_quantity_change_is_resent(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order, mes_status="PENDING")
        production.product_qty = 15
        self.assertEqual((work_order.state, work_order.planned_qty), ("to_send", 15))
        result = [{"row": 1, "order_no": work_order.name, "id": 77, "action": "updated"}]
        with self.mock_openmes(import_report(updated=1, results=result)) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["planned_qty"], 15.0)
        self.assertEqual((work_order.state, work_order.mes_planned_qty), ("sent", 15))

    def test_change_in_production_becomes_change_request_on_the_mo(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order, mes_planned_qty=10)
        production.product_qty = 12
        result = [{"row": 1, "order_no": work_order.name, "id": 77, "action": "change_requested",
                   "change_request_id": 5}]
        with self.mock_openmes(import_report(results=result)):
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual((work_order.change_request_id, work_order.change_request_type), (5, "CHANGE"))
        self.assertEqual(work_order.change_request_status, "SUBMITTED")
        self.assertIn("change request", self._messages(production))

    def test_deadline_change_is_resent_but_noop_write_is_not(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order, mes_status="PENDING")
        production.product_qty = 10
        production.date_deadline = production.date_deadline
        self.assertEqual(work_order.state, "sent")
        production.date_deadline = "2027-01-15 10:00:00"
        self.assertEqual(work_order.state, "to_send")

    def test_change_on_finished_work_order_is_not_sent(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order, mes_status="DONE")
        production.product_qty = 12
        self.assertEqual(work_order.state, "sent")

    # ── Cancellation ─────────────────────────────────────────────────────────

    def test_cancel_unsent_is_local(self):
        production = self._create_mo(self.bun, 10)
        with self.mock_openmes():
            production.action_cancel()
        self.assertEqual(production.openmes_work_order_ids.state, "cancelled")

    def test_cancel_sent_is_sent_to_openmes(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order, mes_status="PENDING")
        production.action_cancel()
        self.assertTrue(work_order.pending_cancel)
        self.assertIn("is being sent to OpenMES", self._messages(production))
        response = FakeResponse(200, {"data": {"result": "cancelled", "work_order": {"id": 77}}})
        with self.mock_openmes(response) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertTrue(calls[0]["url"].endswith(f"/work-orders/{work_order.name}/cancel"))
        self.assertEqual(calls[0]["params"], {"by": "order_no"})
        self.assertIn(production.name, calls[0]["json"]["reason"])
        self.assertIn("Manufacturing order", calls[0]["json"]["reason"])
        self.assertEqual((work_order.state, work_order.mes_status), ("cancelled", "CANCELLED"))
        self.assertFalse(work_order.pending_cancel)

    def test_cancel_in_production_becomes_change_request(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        production.action_cancel()
        response = FakeResponse(202, {"data": {"result": "change_requested", "change_request_id": 12,
                                               "change_request": {"status": "SUBMITTED"}}})
        with self.mock_openmes(response):
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual((work_order.change_request_id, work_order.change_request_type), (12, "CANCEL"))
        self.assertFalse(work_order.pending_cancel)

    def test_cancel_errors(self):
        productions = [self._create_mo(self.bun, 10) for _i in range(3)]
        work_orders = [p.openmes_work_order_ids for p in productions]
        for work_order in work_orders:
            self._sent(work_order)
        for production in productions:
            production.action_cancel()

        def boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        conflict, missing, down = work_orders
        with self.mock_openmes(FakeResponse(409, {"message": "Work order is already DONE."})):
            conflict._openmes_send_cancellations()
        self.assertFalse(conflict.pending_cancel)
        self.assertIn("did not cancel", self._messages(productions[0]))
        with self.mock_openmes(FakeResponse(404, {"message": "Not found"})):
            missing._openmes_send_cancellations()
        self.assertEqual(missing.state, "cancelled")
        with self.mock_openmes(boom):
            down._openmes_send_cancellations()
        self.assertTrue(down.pending_cancel)
        self.assertEqual(down.attempt_count, 1)

    # ── DONE in OpenMES closes the MO ────────────────────────────────────────

    def test_done_full_quantity_marks_mo_done(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        self._pull(self._done_row(work_order, 10))
        self.assertEqual(work_order.mes_status, "DONE")
        self.assertEqual(production.state, "done")
        self.assertEqual(production.qty_producing, 10)
        self.assertFalse(work_order.last_error)
        self.assertEqual(self.env["mrp.production"].search_count([("procurement_group_id", "=", production.procurement_group_id.id)]), 1)
        self.assertIn("Marked as done", self._messages(production))

    def test_done_under_produced_creates_backorder_with_its_own_work_order(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        self._pull(self._done_row(work_order, 6))
        self.assertEqual(production.state, "done")
        self.assertEqual(production.qty_produced, 6)
        backorder = production.procurement_group_id.mrp_production_ids - production
        self.assertEqual(len(backorder), 1)
        self.assertEqual(backorder.product_qty, 4)
        self.assertEqual(backorder.state, "confirmed")
        backorder_wo = backorder.openmes_work_order_ids
        self.assertEqual(len(backorder_wo), 1)
        self.assertEqual((backorder_wo.state, backorder_wo.planned_qty), ("to_send", 4))
        self.assertEqual(backorder_wo.name, "ODOO-" + backorder.name.replace("/", "-"))
        self.assertEqual(work_order.state, "sent", "the original order is not resent")
        self.assertFalse(work_order.last_error)
        self.assertEqual(work_order.planned_qty, 6, "the backorder carries the rest")
        self.assertFalse(work_order.qty_discrepancy)

    def _document_cron_triggers(self):
        cron = self.env.ref("openmes_connector.ir_cron_openmes_stock_documents")
        return self.env["ir.cron.trigger"].sudo().search_count([("cron_id", "=", cron.id)])

    def test_close_triggers_the_stock_document_cron(self):
        # Documents waiting for this MO stop holding back the stock snapshot.
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        before = self._document_cron_triggers()
        self._pull(self._done_row(work_order, 10))
        self.assertEqual(production.state, "done")
        self.assertEqual(self._document_cron_triggers(), before + 1)

    def test_failed_close_does_not_trigger_the_stock_document_cron(self):
        production = self._create_mo(self.bread, 10)  # lot-tracked yeast, no lot given
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        before = self._document_cron_triggers()
        self._pull(self._done_row(work_order, 10))
        self.assertNotEqual(production.state, "done")
        self.assertEqual(self._document_cron_triggers(), before)

    def test_close_error_is_logged_and_does_not_break_the_cron(self):
        failing = self._create_mo(self.bread, 10)  # lot-tracked yeast, no lot given
        fine = self._create_mo(self.bun, 10)
        bread_wo, bun_wo = failing.openmes_work_order_ids, fine.openmes_work_order_ids
        self._sent(bread_wo)
        self._sent(bun_wo, mes_id=78)
        self._pull(self._done_row(bread_wo, 10), self._done_row(bun_wo, 10, id=78))
        self.assertEqual(bread_wo.mes_status, "DONE")
        self.assertNotEqual(failing.state, "done")
        self.assertEqual(failing.qty_producing, 10, "the produced quantity is prefilled")
        self.assertIn("could not mark the manufacturing order as done", bread_wo.last_error)
        self.assertIn("could not mark the manufacturing order as done", self._messages(failing))
        self.assertEqual(fine.state, "done", "the next row of the feed is still applied")

    def test_nothing_produced_leaves_mo_open(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        self._pull(self._done_row(work_order, 0))
        self.assertNotEqual(production.state, "done")
        self.assertIn("nothing produced", self._messages(production))
        self.assertTrue(work_order.last_error)

    def test_retry_close_after_fixing_the_cause(self):
        production = self._create_mo(self.bread, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        self._pull(self._done_row(work_order, 10))
        self.assertNotEqual(production.state, "done")

        mrp_user = new_test_user(self.env, "om_mrp_user_retry", groups="mrp.group_mrp_user")
        with self.assertRaises(AccessError):
            work_order.with_user(mrp_user).action_openmes_finish_production()

        lot = self.env["stock.lot"].create({"name": "YEAST-LOT-1", "product_id": self.yeast.id})
        yeast_move = production.move_raw_ids.filtered(lambda m: m.product_id == self.yeast)
        yeast_move.move_line_ids.lot_id = lot
        manager = new_test_user(self.env, "om_mrp_manager_retry", groups="mrp.group_mrp_manager")
        action = work_order.with_user(manager).action_openmes_finish_production()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(production.state, "done")
        self.assertFalse(work_order.last_error)

    def test_done_for_mo_already_closed_in_odoo_does_nothing(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        production.action_cancel()
        work_order.pending_cancel = False
        self._pull(self._done_row(work_order, 10))
        self.assertEqual(production.state, "cancel")
        self.assertFalse(work_order.last_error)

    def test_mo_work_orders_still_close_after_switching_back(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        self._set_source("sale_lines")
        self._pull(self._done_row(work_order, 10))
        self.assertEqual(production.state, "done")

    def test_done_over_produced_closes_without_backorder(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        self._pull(self._done_row(work_order, 12))
        self.assertEqual(production.state, "done")
        self.assertEqual(production.qty_produced, 12)
        self.assertEqual(production.procurement_group_id.mrp_production_ids, production, "no backorder")
        self.assertFalse(work_order.last_error)

    def test_unexpected_close_exception_does_not_stop_the_pull(self):
        failing = self._create_mo(self.bun, 10)
        fine = self._create_mo(self.bun, 10)
        failing_wo, fine_wo = failing.openmes_work_order_ids, fine.openmes_work_order_ids
        self._sent(failing_wo)
        self._sent(fine_wo, mes_id=78)
        original = type(failing).button_mark_done

        def button_mark_done(productions):
            if failing in productions:
                raise ValueError("internal detail")
            return original(productions)

        with patch.object(type(failing), "button_mark_done", button_mark_done), \
                self.assertLogs("odoo.addons.openmes_connector.models.openmes_work_order", "ERROR"):
            self._pull(self._done_row(failing_wo, 10), self._done_row(fine_wo, 10, id=78))
        self.assertNotEqual(failing.state, "done")
        self.assertIn("server log", failing_wo.last_error)
        self.assertNotIn("internal detail", failing_wo.last_error)
        self.assertEqual(fine.state, "done")

    # ── Splits and closes done in Odoo ───────────────────────────────────────

    def _close_partly_in_odoo(self, production, qty):
        production.qty_producing = qty
        production._set_qty_producing()
        production.with_context(skip_backorder=True, mo_ids_to_backorder=production.ids).button_mark_done()
        return production.procurement_group_id.mrp_production_ids - production

    def test_backorder_of_mo_outside_openmes_is_not_sent(self):
        self._set_source("sale_lines")
        production = self._create_mo(self.bun, 10)
        self._set_source("manufacturing_orders")
        backorder = self._close_partly_in_odoo(production, 4)
        self.assertEqual(len(backorder), 1)
        self.assertFalse((production | backorder).openmes_work_order_ids)

    def test_split_of_mo_outside_openmes_is_not_sent(self):
        self._set_source("sale_lines")
        production = self._create_mo(self.bun, 10)
        self._set_source("manufacturing_orders")
        productions = production._split_productions({production: [3, 7]})
        self.assertEqual(len(productions), 2)
        self.assertFalse(productions.openmes_work_order_ids)

    def test_split_of_mo_in_openmes_sends_the_new_part(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order, mes_status="PENDING")
        productions = production._split_productions({production: [3, 7]})
        new = productions - production
        self.assertEqual(len(production.openmes_work_order_ids), 1, "the original keeps its one work order")
        self.assertEqual((work_order.state, work_order.planned_qty), ("to_send", 3))
        self.assertEqual(len(new.openmes_work_order_ids), 1)
        self.assertEqual(new.openmes_work_order_ids.planned_qty, 7)

    def test_mo_with_open_work_order_cannot_be_deleted(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order, mes_status="PENDING")
        production.action_cancel()
        self.assertTrue(work_order.pending_cancel)
        with self.assertRaises(UserError):
            production.unlink()
        work_order.write({"pending_cancel": False, "state": "cancelled", "mes_status": "CANCELLED"})
        production.unlink()
        self.assertFalse(work_order.exists())

    def test_mo_with_unsent_work_order_can_be_deleted(self):
        production = self._create_mo(self.bun, 10)
        production.action_cancel()
        production.unlink()
        self.assertFalse(production.exists())

    # ── Review round 2 ───────────────────────────────────────────────────────

    def test_commitment_date_of_mto_sale_order_resends_mo_work_order(self):
        order, production = self._mto_order(self.bun, 5)
        work_order = production.openmes_work_order_ids
        self._sent(work_order, mes_status="PENDING")
        order.commitment_date = "2027-03-10 10:00:00"
        self.assertEqual(production.date_deadline.date().isoformat(), "2027-03-10")
        self.assertEqual(work_order.state, "to_send")
        self.assertEqual(work_order._openmes_prepare_payload()["due_date"], "2027-03-10")
        self.assertEqual(self.env["openmes.work.order"].search([("partner_id", "=", self.partner.id)]), work_order,
                         "the customer is searchable")

    def test_produced_quantity_is_not_rounded_up(self):
        production = self._create_mo(self.bun, 1, uom=self.uom_dozen)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        self._pull(self._done_row(work_order, 7))
        expected = float_round(7 / 12, precision_rounding=self.uom_dozen.rounding, rounding_method="HALF-UP")
        self.assertAlmostEqual(production.qty_producing, expected)
        self.assertEqual(production.state, "done")
        self.assertTrue(production.procurement_group_id.mrp_production_ids - production, "backorder for the rest")

    def test_update_queued_when_openmes_finishes_is_not_sent(self):
        production = self._create_mo(self.bun, 10)
        work_order = production.openmes_work_order_ids
        self._sent(work_order)
        production.product_qty = 12
        self.assertEqual(work_order.state, "to_send")
        self._pull(self._done_row(work_order, 10))
        with self.mock_openmes():
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual(work_order.state, "sent")
        self.assertFalse(work_order.last_error)

    def test_split_of_mo_with_cancelled_work_order_is_not_sent(self):
        production = self._create_mo(self.bun, 10)
        production.openmes_work_order_ids.action_cancel()
        self.assertEqual(production.openmes_work_order_ids.state, "cancelled")
        productions = production._split_productions({production: [3, 7]})
        self.assertFalse((productions - production).openmes_work_order_ids)

    # ── Access ───────────────────────────────────────────────────────────────

    def test_mrp_user_confirms_and_reads_but_cannot_send(self):
        mrp_user = new_test_user(self.env, "om_mrp_user", groups="mrp.group_mrp_user")
        production = self._create_mo(self.bun, 5, confirm=False).with_user(mrp_user)
        production.action_confirm()
        self.assertEqual(production.openmes_work_order_count, 1, "created with sudo on confirmation")
        action = production.action_view_openmes_work_orders()
        work_order = self.env["openmes.work.order"].with_user(mrp_user).search(action["domain"])
        self.assertEqual(len(work_order), 1)
        self.assertTrue(work_order.read(["name", "source_name", "production_id", "partner_id"]))
        with self.assertRaises(AccessError):
            work_order.action_send()
        with self.assertRaises(AccessError):
            work_order.action_cancel()

        manager = new_test_user(self.env, "om_mrp_manager", groups="mrp.group_mrp_manager")
        with self.mock_openmes(import_report(imported=1)):
            result = work_order.with_user(manager).action_send()
        self.assertEqual(result["params"]["type"], "success")
        self.assertEqual(work_order.state, "sent")

    def test_salesman_reads_mo_work_order_of_the_order(self):
        salesman = new_test_user(self.env, "om_salesman_mo", groups="sales_team.group_sale_salesman_all_leads")
        order, production = self._mto_order(self.bun, 3)
        work_order = production.openmes_work_order_ids.with_user(salesman)
        self.assertEqual(work_order.read(["source_name"])[0]["source_name"], production.name)
        self.assertEqual(order.with_user(salesman).openmes_work_order_count, 1)
