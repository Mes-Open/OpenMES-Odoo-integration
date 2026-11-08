import requests

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from .common import FakeResponse, OpenmesCase


@tagged("post_install", "-at_install")
class TestQuantityAttention(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.salesman = new_test_user(cls.env, "om_attn_salesman", groups="sales_team.group_sale_salesman")
        cls.sale_manager = new_test_user(cls.env, "om_attn_sale_manager", groups="sales_team.group_sale_manager")
        cls.mrp_user = new_test_user(cls.env, "om_attn_mrp_user", groups="mrp.group_mrp_user")
        cls.mrp_manager = new_test_user(cls.env, "om_attn_mrp_manager", groups="mrp.group_mrp_manager")
        cls.todo = cls.env.ref("mail.mail_activity_data_todo")
        cls.dough = cls.env["product.product"].create({
            "name": "Dough", "default_code": "DOUGH-01", "openmes_manufacture": True,
            "uom_id": cls.uom_kg.id,
        })
        cls.bun = cls.env["product.product"].create({
            "name": "Bun", "default_code": "BUN-01", "is_storable": True, "openmes_manufacture": True,
        })
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.bun.product_tmpl_id.id,
            "product_qty": 10,
            "bom_line_ids": [(0, 0, {"product_id": cls.flour.id, "product_qty": 1})],
        })

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _page(rows):
        return FakeResponse(200, {"data": rows, "meta": {"next_cursor": None}})

    def _work_order(self, qty=60, product=None, user=None, **vals):
        order = self._create_order([(product or self.bread, qty, {})])
        order.user_id = user if user is not None else self.salesman
        order.action_confirm()
        work_order = order.openmes_work_order_ids
        work_order.write(dict({
            "state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS", "mes_planned_qty": qty,
            "sent_date": "2026-11-01 08:00:00",
        }, **vals))
        return work_order

    def _with_change_request(self, work_order, cr_id=9, kind="CHANGE", mes_qty=50):
        work_order.write({"change_request_id": cr_id, "change_request_type": kind,
                          "change_request_status": "SUBMITTED", "mes_planned_qty": mes_qty})
        return work_order

    def _cr_row(self, cr_id=9, status="REJECTED", kind="CHANGE", reason="No capacity", proposed=60):
        row = {"id": cr_id, "type": kind, "status": status, "rejection_reason": reason,
               "updated_at": "2026-11-02T12:00:00+00:00"}
        if proposed is not None:
            row["proposed"] = {"planned_qty": proposed}
        return row

    def _pull_change_requests(self, *responses):
        with self.mock_openmes(*responses):
            self.env["openmes.work.order"]._openmes_pull_change_requests(self.env["openmes.client"])

    def _pull_status(self, *responses):
        with self.mock_openmes(*responses):
            self.env["openmes.work.order"]._openmes_pull_orders(self.env["openmes.client"])

    def _done_row(self, work_order, produced, planned=None):
        return {"order_no": work_order.name, "id": work_order.mes_id or 77, "status": "DONE",
                "produced_qty": produced, "planned_qty": planned if planned is not None else work_order.mes_planned_qty,
                "completed_at": "2026-11-02T11:30:00+00:00", "updated_at": "2026-11-02T11:30:00+00:00"}

    def _activities(self, record):
        return self.env["mail.activity"].search([("res_model", "=", record._name), ("res_id", "=", record.id)])

    def _messages(self, record):
        return " ".join(m.body or "" for m in record.message_ids)

    # ── Rejected change requests ─────────────────────────────────────────────

    def test_rejected_change_creates_one_activity_for_the_salesman(self):
        work_order = self._with_change_request(self._work_order(60))
        order = work_order.sale_order_id
        self._pull_change_requests(self._page([self._cr_row()]))
        activity = self._activities(order)
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.activity_type_id, self.todo)
        self.assertEqual(activity.user_id, self.salesman)
        self.assertIn("No capacity", activity.note)
        self.assertIn("requested 60", activity.note)
        self.assertIn("keeps planning 50", activity.note)
        self.assertEqual(work_order.change_request_status, "REJECTED")
        self.assertEqual(work_order.qty_discrepancy, "plan")
        self.assertEqual(work_order.attention_change_request_id, 9)
        self.assertIn("No capacity", self._messages(order), "the chatter message stays")

        # Same feed again (e.g. the overlap window), then a stray re-open.
        work_order.change_request_status = "SUBMITTED"
        self._pull_change_requests(self._page([self._cr_row()]))
        self.assertEqual(len(self._activities(order)), 1, "no duplicate for the same request")
        work_order._openmes_apply_change_request(self._cr_row())
        self.assertEqual(len(self._activities(order)), 1)

    def test_activity_marked_done_is_not_created_again(self):
        work_order = self._with_change_request(self._work_order(60))
        order = work_order.sale_order_id
        self._pull_change_requests(self._page([self._cr_row()]))
        self._activities(order).action_feedback(feedback="Talked to the customer")
        self.assertFalse(self._activities(order))
        work_order.change_request_status = "SUBMITTED"
        self._pull_change_requests(self._page([self._cr_row()]))
        self.assertFalse(self._activities(order))

    def test_rejected_cancellation_creates_one_activity(self):
        work_order = self._work_order(60)
        order = work_order.sale_order_id
        self._with_change_request(work_order, cr_id=12, kind="CANCEL", mes_qty=60)
        order._action_cancel()
        work_order.pending_cancel = False  # the cancellation was sent as a request
        self._pull_change_requests(self._page([self._cr_row(12, kind="CANCEL", reason="Already packed", proposed=None)]))
        activity = self._activities(order)
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.user_id, self.salesman)
        self.assertIn("cancellation", activity.note)
        self.assertIn("Already packed", activity.note)
        self.assertIn("keeps producing 60", activity.note)
        self.assertFalse(work_order.qty_discrepancy, "a rejected cancellation is not a quantity difference")
        work_order.change_request_status = "SUBMITTED"
        self._pull_change_requests(self._page([self._cr_row(12, kind="CANCEL", reason="Already packed", proposed=None)]))
        self.assertEqual(len(self._activities(order)), 1)

    def test_each_rejected_request_gets_its_own_activity(self):
        work_order = self._with_change_request(self._work_order(60))
        order = work_order.sale_order_id
        self._pull_change_requests(self._page([self._cr_row()]))
        # The salesperson asks again; a new request is opened and rejected too.
        work_order.write({"change_request_id": 10, "change_request_status": "SUBMITTED"})
        self._pull_change_requests(self._page([self._cr_row(10, reason="Still no capacity")]))
        activities = self._activities(order)
        self.assertEqual(len(activities), 2)
        self.assertTrue(any("Still no capacity" in a.note for a in activities))

    def test_approved_and_applied_requests_create_nothing(self):
        work_order = self._with_change_request(self._work_order(60))
        self._pull_change_requests(self._page([self._cr_row(status="APPROVED")]))
        self.assertFalse(self._activities(work_order.sale_order_id))
        self.assertFalse(work_order.qty_discrepancy, "an approved request is still open")
        work_order.mes_planned_qty = 60  # the status pull after OpenMES applied it
        self._pull_change_requests(self._page([self._cr_row(status="APPLIED")]))
        self.assertFalse(self._activities(work_order.sale_order_id))
        self.assertFalse(work_order.qty_discrepancy)

    def test_rejection_reason_is_escaped(self):
        work_order = self._with_change_request(self._work_order(60))
        self._pull_change_requests(self._page([self._cr_row(reason="<script>x</script>")]))
        note = self._activities(work_order.sale_order_id).note
        self.assertNotIn("<script>", note)
        self.assertIn("&lt;script&gt;", note)

    def test_rejection_without_reason_or_proposal(self):
        work_order = self._with_change_request(self._work_order(60))
        self._pull_change_requests(self._page([self._cr_row(reason=None, proposed=None)]))
        note = self._activities(work_order.sale_order_id).note
        self.assertIn("no reason given", note)
        self.assertIn("requested 60", note, "falls back to the Odoo quantity")

    # ── DONE with a different quantity ───────────────────────────────────────

    def test_done_over_and_under_production(self):
        for produced, summary, diff in ((62, "OpenMES: over-production", "+2"), (58, "OpenMES: under-production", "-2")):
            with self.subTest(produced=produced):
                work_order = self._work_order(60)
                order = work_order.sale_order_id
                self._pull_status(self._page([self._done_row(work_order, produced)]))
                activity = self._activities(order)
                self.assertEqual(len(activity), 1)
                self.assertEqual(activity.summary, summary)
                self.assertEqual(activity.user_id, self.salesman)
                self.assertIn(f"with {produced} Units instead of 60 Units ({diff} Units)", activity.note)
                self.assertIn("delivery and the invoice", activity.note)
                self.assertEqual(work_order.qty_discrepancy, "production")
                self.assertTrue(work_order.done_activity_scheduled)
                # A repeated DONE row (overlap window) changes nothing.
                work_order._openmes_apply_status(self._done_row(work_order, produced))
                self.assertEqual(len(self._activities(order)), 1)

    def test_done_with_equal_quantity_creates_nothing(self):
        work_order = self._work_order(60)
        self._pull_status(self._page([self._done_row(work_order, 60)]))
        self.assertEqual(work_order.mes_status, "DONE")
        self.assertFalse(self._activities(work_order.sale_order_id))
        self.assertFalse(work_order.qty_discrepancy)

    def test_done_compares_with_the_odoo_quantity(self):
        # A rejected 50 → 60: OpenMES makes 50, the sale line still expects 60.
        work_order = self._work_order(60, mes_planned_qty=50)
        self._pull_status(self._page([self._done_row(work_order, 50, planned=50)]))
        activity = self._activities(work_order.sale_order_id)
        self.assertEqual(len(activity), 1)
        self.assertIn("with 50 Units instead of 60 Units", activity.note)

    def test_rounding_tolerance_follows_the_unit(self):
        # kg: 10.0004 is 10.000 kg, 10.05 is not.
        same = self._work_order(10, product=self.dough)
        self._pull_status(self._page([self._done_row(same, 10.0004)]))
        self.assertEqual(same.mes_status, "DONE")
        self.assertFalse(self._activities(same.sale_order_id))
        self.assertFalse(same.qty_discrepancy)

        other = self._work_order(10, product=self.dough)
        self._pull_status(self._page([self._done_row(other, 10.05)]))
        self.assertEqual(len(self._activities(other.sale_order_id)), 1)
        self.assertEqual(other.qty_discrepancy, "production")
        self.assertIn("10.05 kg instead of 10 kg (+0.05 kg)", self._activities(other.sale_order_id).note)

        # Odoo 19: every unit shares the "Product Unit" precision (0.01), a
        # unit of its own (10 units per crate) no longer has its own rounding.
        crate = self.env["uom.uom"].create({
            "name": "Crate", "relative_factor": 10, "relative_uom_id": self.uom_unit.id,
        })
        crates = self.env["product.product"].create({
            "name": "Crated rolls", "default_code": "CRATE-01", "openmes_manufacture": True,
            "uom_id": crate.id,
        })
        close = self._work_order(24, product=crates)
        self._pull_status(self._page([self._done_row(close, 24.004)]))
        self.assertAlmostEqual(close.produced_qty, 24.0)
        self.assertFalse(self._activities(close.sale_order_id))
        self.assertFalse(close.qty_discrepancy)
        off = self._work_order(24, product=crates)
        self._pull_status(self._page([self._done_row(off, 24.01)]))
        self.assertEqual(len(self._activities(off.sale_order_id)), 1)
        self.assertEqual(off.qty_discrepancy, "production")

        units = self._work_order(24)
        units.write({"mes_status": "DONE", "produced_qty": 24.0000001})
        self.assertFalse(units.qty_discrepancy)
        units.write({"mes_status": "IN_PROGRESS", "produced_qty": 0, "mes_planned_qty": 24.0000001})
        self.assertFalse(units.qty_discrepancy)

    # ── Assignee ─────────────────────────────────────────────────────────────

    def test_no_valid_assignee_flags_without_activity(self):
        other_company = self.env["res.company"].create({"name": "Other bakery"})
        outsider = new_test_user(
            self.env, "om_attn_outsider", groups="sales_team.group_sale_salesman_all_leads",
            company_id=other_company.id, company_ids=[(6, 0, other_company.ids)],
        )
        for user in (self.env["res.users"], outsider):
            with self.subTest(user=user.login):
                work_order = self._work_order(60)
                order = work_order.sale_order_id
                order.sudo().user_id = user
                self._pull_status(self._page([self._done_row(work_order, 58)]))
                self.assertEqual(work_order.mes_status, "DONE")
                self.assertFalse(self._activities(order))
                self.assertEqual(work_order.qty_discrepancy, "production")
                self.assertIn(work_order.name, self._messages(order))

    # ── Manufacturing orders mode ────────────────────────────────────────────

    def _mo_work_order(self, qty=10):
        self._configure(order_source="manufacturing_orders")
        production = self.env["mrp.production"].create({
            # Odoo 20: mrp.bom(.line), mrp.production and mrp.workcenter.capacity renamed product_uom_id to uom_id.
            "product_id": self.bun.id, "product_qty": qty, "uom_id": self.bun.uom_id.id,
            "user_id": self.mrp_user.id,
        })
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS", "mes_planned_qty": qty,
                          "sent_date": "2026-11-01 08:00:00"})
        return production, work_order

    def test_mo_done_activity_lands_on_the_mo(self):
        production, work_order = self._mo_work_order(10)
        self._pull_status(self._page([self._done_row(work_order, 12)]))
        self.assertEqual(production.state, "done", "the MO still closes")
        activity = self._activities(production)
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.user_id, self.mrp_user)
        self.assertIn("with 12 Units instead of 10 Units", activity.note)

    def test_mo_under_production_with_backorder_is_no_difference(self):
        production, work_order = self._mo_work_order(10)
        self._pull_status(self._page([self._done_row(work_order, 6)]))
        self.assertEqual(production.state, "done")
        backorder = production._openmes_backorder_family() - production
        self.assertEqual(backorder.product_qty, 4)
        self.assertEqual(backorder.openmes_work_order_ids.planned_qty, 4)
        # The backorder carries the rest: no "check the delivery and the invoice".
        self.assertEqual(work_order.planned_qty, 6)
        self.assertFalse(work_order.qty_discrepancy)
        self.assertFalse(self._activities(production))
        self.assertIn("the rest is in backorder " + backorder.name, self._messages(production))

    def test_mo_failed_close_keeps_the_difference_until_closed(self):
        self._configure(order_source="manufacturing_orders")
        production = self.env["mrp.production"].create({
            "product_id": self.bread.id, "product_qty": 10, "user_id": self.mrp_user.id,
        })  # lot-tracked yeast, no lot: the close fails
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 78, "mes_status": "IN_PROGRESS", "mes_planned_qty": 10})
        self._pull_status(self._page([self._done_row(work_order, 6)]))
        self.assertNotEqual(production.state, "done")
        self.assertEqual(work_order.qty_discrepancy, "production")
        self.assertEqual(len(self._activities(production)), 1)

        lot = self.env["stock.lot"].create({"name": "YEAST-LOT-F4", "product_id": self.yeast.id})
        production.move_raw_ids.filtered(lambda m: m.product_id == self.yeast).move_line_ids.lot_id = lot
        work_order.with_user(self.mrp_manager).action_openmes_finish_production()
        self.assertEqual(production.state, "done")
        self.assertEqual(work_order.planned_qty, 6)
        self.assertFalse(work_order.qty_discrepancy)

    def test_mo_rejected_change_targets_the_mo(self):
        production, work_order = self._mo_work_order(10)
        work_order.planned_qty = 12
        self._with_change_request(work_order, mes_qty=10)
        self._pull_change_requests(self._page([self._cr_row(proposed=12)]))
        activity = self._activities(production)
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.user_id, self.mrp_user)
        self.assertIn("keeps planning 10", activity.note)
        self.assertEqual(work_order.qty_discrepancy, "plan")

    # ── Accept OpenMES quantity ──────────────────────────────────────────────

    def test_sale_manager_accepts_planned_quantity(self):
        work_order = self._with_change_request(self._work_order(60))
        self._pull_change_requests(self._page([self._cr_row()]))
        line = work_order.sale_line_id
        with self.mock_openmes():  # nothing is sent
            result = work_order.with_user(self.sale_manager).action_openmes_accept_quantity()
        self.assertEqual(result["params"]["type"], "success")
        self.assertEqual(work_order.planned_qty, 50)
        self.assertFalse(work_order.qty_discrepancy)
        self.assertEqual(work_order.state, "sent")
        self.assertEqual(line.product_uom_qty, 60, "the sale order is not changed")
        self.assertIn("set to the OpenMES quantity 50", self._messages(work_order.sale_order_id))
        self.assertEqual(len(self._activities(work_order.sale_order_id)), 1, "the to-do stays for the person")

    def test_accept_produced_quantity(self):
        work_order = self._work_order(60)
        self._pull_status(self._page([self._done_row(work_order, 58)]))
        with self.mock_openmes():
            work_order.with_user(self.sale_manager).action_openmes_accept_quantity()
        self.assertEqual(work_order.planned_qty, 58)
        self.assertFalse(work_order.qty_discrepancy)
        self.assertEqual(work_order.sale_line_id.product_uom_qty, 60)

    def test_mrp_manager_accepts_on_mo(self):
        production, work_order = self._mo_work_order(10)
        work_order.write({"mes_planned_qty": 8})
        self.assertEqual(work_order.qty_discrepancy, "plan")
        with self.mock_openmes():
            work_order.with_user(self.mrp_manager).action_openmes_accept_quantity()
        self.assertEqual(work_order.planned_qty, 8)
        self.assertEqual(production.product_qty, 10, "the MO is not changed")
        self.assertFalse(work_order.qty_discrepancy)

    def test_accept_needs_a_manager(self):
        work_order = self._work_order(60, mes_planned_qty=50)
        self.assertEqual(work_order.qty_discrepancy, "plan")
        for user in (self.salesman, self.mrp_user):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                work_order.with_user(user).action_openmes_accept_quantity()
        self.assertEqual(work_order.planned_qty, 60)

    def test_accept_without_difference_is_refused(self):
        work_order = self._work_order(60)
        with self.assertRaises(UserError):
            work_order.with_user(self.sale_manager).action_openmes_accept_quantity()

    # ── When there is no difference ──────────────────────────────────────────

    def test_no_flag_while_in_flight_or_finished_otherwise(self):
        work_order = self._work_order(60, mes_planned_qty=50)
        self.assertEqual(work_order.qty_discrepancy, "plan")
        cases = [
            {"change_request_id": 9, "change_request_type": "CHANGE", "change_request_status": "SUBMITTED"},
            {"state": "to_send"},
            {"mes_planned_qty": 0},
            {"state": "cancelled"},
            {"pending_cancel": True},
            {"mes_status": "REJECTED"},
            {"mes_status": "CANCELLED"},
        ]
        for vals in cases:
            with self.subTest(vals=vals):
                original = {key: work_order[key] for key in vals}
                work_order.write(vals)
                self.assertFalse(work_order.qty_discrepancy)
                work_order.write(original)
                self.assertEqual(work_order.qty_discrepancy, "plan")

    def test_needs_attention_filter(self):
        flagged = self._work_order(60, mes_planned_qty=50)
        fine = self._work_order(60)
        done = self._work_order(60, mes_status="DONE", produced_qty=59)
        found = self.env["openmes.work.order"].search([("qty_discrepancy", "!=", False)])
        self.assertIn(flagged, found)
        self.assertIn(done, found)
        self.assertNotIn(fine, found)

    # ── OpenMES errors ───────────────────────────────────────────────────────

    def test_feed_errors_create_nothing(self):
        def timeout(*args, **kwargs):
            raise requests.Timeout("slow")

        for response in (FakeResponse(500, {"message": "boom"}), timeout):
            with self.subTest(response=response):
                work_order = self._with_change_request(self._work_order(60))
                self._pull_change_requests(response)
                self.assertEqual(work_order.change_request_status, "SUBMITTED")
                self.assertFalse(self._activities(work_order.sale_order_id))

        work_order = self._work_order(60)
        self._pull_status(FakeResponse(401, {"message": "Unauthenticated."}))
        self.assertEqual(work_order.mes_status, "IN_PROGRESS")
        self.assertFalse(self._activities(work_order.sale_order_id))
