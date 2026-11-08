import requests

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from .common import API_KEY, API_URL, FakeResponse, OpenmesCase, import_report


@tagged("post_install", "-at_install")
class TestWorkOrders(OpenmesCase):
    def _confirmed_order(self):
        order = self._create_order([
            (self.bread, 24, {"price_unit": 4.0}),
            (self.cake, 2, {}),
            (self.service, 1, {}),
        ])
        order.action_confirm()
        return order

    # ── Creation on confirmation ─────────────────────────────────────────────

    def test_confirm_creates_work_orders_for_flagged_products_only(self):
        order = self._confirmed_order()
        work_orders = order.openmes_work_order_ids
        self.assertEqual(len(work_orders), 2)
        self.assertEqual(set(work_orders.mapped("state")), {"to_send"})
        bread_wo = work_orders.filtered(lambda w: w.product_id == self.bread)
        bread_line = order.order_line.filtered(lambda l: l.product_id == self.bread)
        self.assertEqual(bread_wo.name, f"ODOO-{order.name}-{bread_line.id}")
        self.assertEqual(bread_wo.line_code, "L1", "default line from settings")
        self.assertEqual(work_orders.filtered(lambda w: w.product_id == self.cake).line_code, "L2")
        self.assertEqual(order.openmes_work_order_count, 2)

    def test_confirm_converts_quantity_to_product_uom(self):
        order = self._create_order([(self.bread, 2, {"product_uom_id": self.uom_dozen.id, "price_unit": 48.0})])
        order.action_confirm()
        self.assertAlmostEqual(order.openmes_work_order_ids.planned_qty, 24.0)

    def test_disabled_integration_creates_nothing(self):
        self._configure(enabled=False)
        order = self._confirmed_order()
        self.assertFalse(order.openmes_work_order_ids)

    def test_salesman_can_confirm_but_not_send(self):
        salesman = new_test_user(self.env, "om_salesman", groups="sales_team.group_sale_salesman")
        order = self._create_order([(self.bread, 1, {})])
        order.user_id = salesman
        order = order.with_user(salesman)
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        self.assertEqual(len(work_order), 1, "work orders are created with sudo on confirmation")
        with self.assertRaises(AccessError):
            work_order.with_user(salesman).action_send()

    # ── Sending ──────────────────────────────────────────────────────────────

    def test_send_success_payload_and_headers(self):
        order = self._confirmed_order()
        order.commitment_date = "2026-12-01 10:00:00"
        order.client_order_ref = "PO-77"
        work_orders = order.openmes_work_order_ids.sorted("id")
        results = [
            {"row": 1, "order_no": work_orders[0].name, "id": 501, "action": "created"},
            {"row": 2, "order_no": work_orders[1].name, "id": 502, "action": "created"},
        ]
        with self.mock_openmes(import_report(imported=2, results=results)) as calls:
            work_orders._openmes_send()

        self.assertEqual(set(work_orders.mapped("state")), {"sent"})
        self.assertEqual(set(work_orders.mapped("mes_status")), {"PENDING"})
        self.assertEqual(work_orders.mapped("mes_id"), [501, 502])
        self.assertEqual(work_orders[0].mes_planned_qty, 24.0)
        call = calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{API_URL}/api/v1/erp/work-orders/import")
        self.assertEqual(call["headers"]["X-Api-Key"], API_KEY)
        self.assertFalse(call["allow_redirects"])
        body = call["json"]
        self.assertEqual(body["strategy"], "update_or_create")
        bread = next(o for o in body["orders"] if o["product_type_code"] == "BREAD-01")
        self.assertEqual(bread["line_code"], "L1")
        self.assertEqual(bread["planned_qty"], 24.0)
        self.assertEqual(bread["customer_order_no"], "PO-77")
        self.assertEqual(bread["due_date"], "2026-12-01")
        self.assertEqual(bread["unit_price"], 4.0)
        self.assertEqual(bread["price_currency"], order.currency_id.name)
        self.assertEqual(bread["external_system"], "odoo")
        bread_line = order.order_line.filtered(lambda l: l.product_id == self.bread)
        self.assertEqual(bread["external_ref"], f"sale.order.line,{bread_line.id}")
        self.assertEqual(bread["customer_name"], "Bakery Customer")
        self.assertEqual(bread["extra_data"], {"sale_order": order.name})

    def test_unit_price_is_rounded_like_openmes_stores_it(self):
        order = self._create_order([(self.bread, 3, {"price_unit": 10.0 / 3})])
        order.action_confirm()
        with self.mock_openmes(import_report(imported=1)) as calls:
            order.openmes_work_order_ids._openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["unit_price"], 3.33)

    def test_change_on_order_in_production_becomes_change_request(self):
        order = self._create_order([(self.bread, 10, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS", "mes_planned_qty": 10})

        order.order_line.product_uom_qty = 15
        self.assertEqual(work_order.state, "to_send", "a quantity change queues a resend")
        self.assertEqual(work_order.planned_qty, 15)

        result = [{"row": 1, "order_no": work_order.name, "id": 77, "action": "change_requested", "change_request_id": 9}]
        with self.mock_openmes(import_report(results=result)) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["planned_qty"], 15.0)
        self.assertEqual(work_order.state, "sent")
        self.assertEqual(work_order.change_request_id, 9)
        self.assertEqual(work_order.change_request_type, "CHANGE")
        self.assertEqual(work_order.change_request_status, "SUBMITTED")
        self.assertEqual(work_order.mes_planned_qty, 10, "OpenMES keeps producing 10 until the change is applied")
        self.assertTrue(any("change request" in (m.body or "") for m in order.message_ids))

    def test_unit_change_on_confirmed_line_resends_converted_quantity(self):
        # Odoo 19 renamed sale.order.line.product_uom to product_uom_id.
        order = self._create_order([(self.bread, 2, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 77, "mes_status": "PENDING", "mes_planned_qty": 2})
        order.order_line.product_uom_id = self.uom_dozen
        self.assertEqual(work_order.state, "to_send", "a unit change queues a resend")
        self.assertAlmostEqual(work_order.planned_qty, 24.0, msg="sent in the product unit")
        with self.mock_openmes(import_report(updated=1)) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["planned_qty"], 24.0)

    def test_change_on_finished_order_is_not_sent(self):
        order = self._create_order([(self.bread, 10, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 77, "mes_status": "DONE"})
        order.order_line.product_uom_qty = 12
        self.assertEqual(work_order.state, "sent")

    def test_send_action_reloads_the_view(self):
        work_orders = self._confirmed_order().openmes_work_order_ids
        with self.mock_openmes(import_report(imported=2)):
            action = work_orders.action_send()
        self.assertEqual(action["params"]["next"]["tag"], "soft_reload")

    def test_send_maps_row_errors_to_records(self):
        work_orders = self._confirmed_order().openmes_work_order_ids.sorted("id")
        response = import_report(
            imported=1,
            errors=[{"row": 2, "field": "line_code", "message": "Line 'L2' not found"}],
        )
        with self.mock_openmes(response):
            work_orders._openmes_send()
        self.assertEqual(work_orders[0].state, "sent")
        self.assertEqual(work_orders[1].state, "error")
        self.assertIn("Line 'L2' not found", work_orders[1].last_error)
        self.assertEqual(work_orders[1].attempt_count, 1)

    def test_skipped_existing_order_counts_as_sent(self):
        work_orders = self._confirmed_order().openmes_work_order_ids
        with self.mock_openmes(import_report(skipped=2)):
            work_orders._openmes_send()
        self.assertEqual(set(work_orders.mapped("state")), {"sent"})

    def test_auth_failure_marks_whole_batch_in_error(self):
        work_orders = self._confirmed_order().openmes_work_order_ids
        with self.mock_openmes(FakeResponse(401, {"message": "Invalid or expired API key."})):
            work_orders._openmes_send()
        self.assertEqual(set(work_orders.mapped("state")), {"error"})
        self.assertIn("API key", work_orders[0].last_error)

    def test_network_error_marks_batch_in_error(self):
        work_orders = self._confirmed_order().openmes_work_order_ids

        def boom(*args, **kwargs):
            raise requests.ConnectionError("connection refused")

        with self.mock_openmes(boom):
            work_orders._openmes_send()
        self.assertEqual(set(work_orders.mapped("state")), {"error"})
        self.assertIn("Cannot reach OpenMES", work_orders[0].last_error)

    def test_local_validation_skips_http(self):
        self.cake.default_code = False
        work_orders = self._confirmed_order().openmes_work_order_ids
        cake_wo = work_orders.filtered(lambda w: w.product_id == self.cake)
        with self.mock_openmes(import_report(imported=1)) as calls:
            work_orders._openmes_send()
        self.assertEqual(len(calls[0]["json"]["orders"]), 1)
        self.assertEqual(cake_wo.state, "error")
        self.assertIn("Internal Reference", cake_wo.last_error)

    def test_chosen_lines_reach_the_payload(self):
        order = self._create_order([(self.cake, 1, {})])
        order.action_confirm()
        with self.mock_openmes(import_report(imported=1)) as calls:
            order.openmes_work_order_ids._openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["line_code"], "L2", "the product's line")

        # No line on the product: the default line picked in Settings.
        self.cake.product_tmpl_id.openmes_line_id = False
        self.env.company.openmes_default_line_id = False
        self.env["res.config.settings"].create({"openmes_default_line_id": self.line_l2.id}).execute()
        self.assertEqual(self.env.company.openmes_default_line_id, self.line_l2)
        order = self._create_order([(self.cake, 1, {})])
        order.action_confirm()
        with self.mock_openmes(import_report(imported=1)) as calls:
            order.openmes_work_order_ids._openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["line_code"], "L2")
        self.env["res.config.settings"].create({"openmes_default_line_id": self.line_l1.id}).execute()
        order = self._create_order([(self.cake, 1, {})])
        order.action_confirm()
        self.assertEqual(order.openmes_work_order_ids.line_code, "L1")

    def test_missing_line_code_is_reported(self):
        self.env.company.openmes_default_line_id = False
        order = self._create_order([(self.bread, 1, {})])
        order.action_confirm()
        with self.mock_openmes():
            order.openmes_work_order_ids._openmes_send()
        self.assertEqual(order.openmes_work_order_ids.state, "error")
        self.assertIn("line code", order.openmes_work_order_ids.last_error)

    def test_cron_stops_retrying_after_max_attempts(self):
        work_orders = self._confirmed_order().openmes_work_order_ids
        work_orders.write({"state": "error", "attempt_count": 5})
        with self.mock_openmes():
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual(set(work_orders.mapped("state")), {"error"})

    # ── Cancellation ─────────────────────────────────────────────────────────

    def _sent(self, work_order, **vals):
        work_order.write(dict({"state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS"}, **vals))

    def test_cancel_order_drops_unsent_and_queues_cancellation_of_sent(self):
        order = self._confirmed_order()
        sent, unsent = order.openmes_work_order_ids.sorted("id")
        self._sent(sent)
        order._action_cancel()
        self.assertEqual(unsent.state, "cancelled")
        self.assertTrue(sent.pending_cancel)
        self.assertEqual(sent.state, "sent")

    def test_cancellation_applied_at_once(self):
        order = self._create_order([(self.bread, 5, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        self._sent(work_order, mes_status="PENDING")
        order._action_cancel()
        response = FakeResponse(200, {"data": {"result": "cancelled", "work_order": {"id": 77, "status": "CANCELLED"}}})
        with self.mock_openmes(response) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        call = calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].endswith(f"/api/v1/erp/work-orders/{work_order.name}/cancel"))
        self.assertEqual(call["params"], {"by": "order_no"})
        self.assertIn(order.name, call["json"]["reason"])
        self.assertEqual(work_order.state, "cancelled")
        self.assertEqual(work_order.mes_status, "CANCELLED")
        self.assertFalse(work_order.pending_cancel)

    def test_cancellation_of_order_in_production_becomes_change_request(self):
        order = self._create_order([(self.bread, 5, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        self._sent(work_order)
        order._action_cancel()
        # OpenMES answers 202 Accepted: the cancellation waits for a manager.
        response = FakeResponse(202, {"data": {
            "result": "change_requested", "change_request_id": 12,
            "change_request": {"id": 12, "type": "CANCEL", "status": "SUBMITTED"},
            "work_order": {"id": 77, "status": "IN_PROGRESS"},
        }})
        with self.mock_openmes(response):
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertFalse(work_order.pending_cancel)
        self.assertEqual(work_order.state, "sent")
        self.assertEqual((work_order.change_request_id, work_order.change_request_type), (12, "CANCEL"))
        self.assertEqual(work_order.change_request_status, "SUBMITTED")

    def test_cancellation_of_finished_order_is_reported(self):
        order = self._create_order([(self.bread, 5, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        self._sent(work_order)
        order._action_cancel()
        with self.mock_openmes(FakeResponse(409, {"message": "Work order is already DONE."})):
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertFalse(work_order.pending_cancel)
        self.assertIn("already DONE", work_order.last_error)
        self.assertTrue(any("did not cancel" in (m.body or "") for m in order.message_ids))

    def test_cancellation_network_error_is_retried(self):
        order = self._create_order([(self.bread, 5, {})])
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        self._sent(work_order)
        order._action_cancel()

        def boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        with self.mock_openmes(boom):
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertTrue(work_order.pending_cancel)
        self.assertEqual(work_order.attempt_count, 1)

    # ── Status pull ──────────────────────────────────────────────────────────

    @staticmethod
    def _page(rows, next_cursor=None):
        return FakeResponse(200, {"data": rows, "meta": {"next_cursor": next_cursor, "has_more": bool(next_cursor)}})

    def test_pull_status_reads_all_statuses_in_one_feed(self):
        work_orders = self._confirmed_order().openmes_work_order_ids.sorted("id")
        work_orders.write({"state": "sent", "mes_status": "PENDING", "sent_date": "2026-11-01 08:00:00"})
        bread, cake = work_orders

        def feed(method, url, params=None, **kwargs):
            self.assertEqual(params["status"], "all")
            if "cursor" not in params:
                return self._page([
                    {"order_no": cake.name, "id": 8, "status": "IN_PROGRESS", "produced_qty": 1, "planned_qty": 2,
                     "updated_at": "2026-11-02T09:00:00+00:00"},
                    {"order_no": "FOREIGN-1", "status": "DONE", "updated_at": "2026-11-02T10:00:00+00:00"},
                ], "c2")
            return self._page([{"order_no": bread.name, "id": 7, "status": "DONE", "produced_qty": 24, "planned_qty": 24,
                                "completed_at": "2026-11-02T11:30:00+00:00", "updated_at": "2026-11-02T11:30:00+00:00"}])

        with self.mock_openmes(feed, feed) as calls:
            self.env["openmes.work.order"]._cron_openmes_pull_status()

        self.assertEqual(calls[0]["params"]["since"], "2026-11-01T07:58:00Z")
        self.assertEqual(calls[1]["params"]["cursor"], "c2")
        self.assertEqual((bread.mes_status, bread.produced_qty, bread.mes_id), ("DONE", 24, 7))
        self.assertEqual(str(bread.completed_at), "2026-11-02 11:30:00")
        self.assertEqual((cake.mes_status, cake.mes_planned_qty), ("IN_PROGRESS", 2))
        self.assertTrue(any(bread.name in (m.body or "") for m in bread.sale_order_id.message_ids))
        self.assertEqual(str(self.env.company.openmes_status_since), "2026-11-02 11:30:00")

    def test_pull_status_falls_back_to_one_status_per_request(self):
        work_order = self._confirmed_order().openmes_work_order_ids[0]
        work_order.write({"state": "sent", "mes_status": "PENDING", "sent_date": "2026-11-01 08:00:00"})
        seen = []

        def old_server(method, url, params=None, **kwargs):
            seen.append(params["status"])
            if params["status"] == "all":
                return FakeResponse(422, {"message": "invalid", "errors": {"status": ["invalid"]}})
            rows = [{"order_no": work_order.name, "status": "DONE", "produced_qty": 3,
                     "updated_at": "2026-11-02T11:30:00+00:00"}] if params["status"] == "DONE" else []
            return self._page(rows)

        with self.mock_openmes(*([old_server] * 10)):
            self.env["openmes.work.order"]._cron_openmes_pull_status()
        self.assertEqual(len(seen), 10)
        self.assertEqual(work_order.mes_status, "DONE")

    def test_pull_change_requests_follows_the_managers_decision(self):
        order = self._create_order([(self.bread, 10, {}), (self.cake, 2, {})])
        order.action_confirm()
        change, cancel = order.openmes_work_order_ids.sorted("id")
        for record, cr_id, kind in ((change, 9, "CHANGE"), (cancel, 12, "CANCEL")):
            record.write({"state": "sent", "mes_id": cr_id, "mes_status": "DONE", "change_request_id": cr_id,
                          "change_request_type": kind, "change_request_status": "SUBMITTED"})

        def feed(method, url, params=None, **kwargs):
            self.assertTrue(url.endswith("/api/v1/erp/change-requests"))
            return self._page([
                {"id": 9, "type": "CHANGE", "status": "APPLIED", "updated_at": "2026-11-02T12:00:00+00:00"},
                {"id": 12, "type": "CANCEL", "status": "REJECTED", "rejection_reason": "Already packed",
                 "updated_at": "2026-11-02T12:05:00+00:00"},
            ])

        with self.mock_openmes(feed):
            self.env["openmes.work.order"]._cron_openmes_pull_status()
        self.assertEqual(change.change_request_status, "APPLIED")
        self.assertEqual(cancel.change_request_status, "REJECTED")
        bodies = " ".join(m.body or "" for m in order.message_ids)
        self.assertIn("applied the change", bodies)
        self.assertIn("Already packed", bodies)
        self.assertEqual(str(self.env.company.openmes_cr_since), "2026-11-02 12:05:00")

    def test_pull_status_noop_without_tracked_orders(self):
        with self.mock_openmes():
            self.env["openmes.work.order"]._cron_openmes_pull_status()
