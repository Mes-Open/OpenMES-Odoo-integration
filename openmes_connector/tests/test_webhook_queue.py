"""The webhook queue: deliveries are stored by the request and applied by the
"OpenMES: process webhook events" cron; and the send that must survive a
webhook event changing the same work order at the same time.

Real serialization conflicts need two committed transactions, which a test
transaction cannot have; they are simulated by raising the psycopg2 errors.
"""

import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import patch

import psycopg2

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from ..models.openmes_webhook import MAX_EVENT_ATTEMPTS, OpenmesWebhook, OpenmesWebhookEvent
from ..models.openmes_work_order import OpenmesWorkOrder
from .common import FakeResponse, OpenmesCase, import_report
from .test_migration import _load

SECRET = "Zx8" + "s" * 37
WEBHOOK_LOGGER = "odoo.addons.openmes_connector.models.openmes_webhook"


def _serialization_failure():
    return psycopg2.errors.SerializationFailure("could not serialize access due to concurrent update")


class QueueCase(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._configure(webhook_id=12, webhook_secret=SECRET)
        cls.queue_cron = cls.env.ref("openmes_connector.ir_cron_openmes_webhook_events")
        cls.send_cron = cls.env.ref("openmes_connector.ir_cron_openmes_send")

    def _deliver(self, event, data, delivery):
        raw = json.dumps({"event": event, "data": data}).encode()
        ts = str(int(time.time()))
        signature = "sha256=" + hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
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

    def _event(self, delivery):
        return self.env["openmes.webhook.event"].sudo().search([("delivery_id", "=", delivery)])

    def _fail_once(self, model, method, error):
        original = getattr(model, method)
        calls = []

        def once(records, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise error
            return original(records, *args, **kwargs)

        return patch.object(model, method, once)

    def _triggers(self, cron, later_than=None):
        domain = [("cron_id", "=", cron.id)]
        if later_than:
            domain.append(("call_at", ">", fields.Datetime.now() + later_than))
        return self.env["ir.cron.trigger"].sudo().search_count(domain)


@tagged("post_install", "-at_install")
class TestWebhookQueue(QueueCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        order = cls._create_order(cls, [(cls.bread, 24, {})])
        order.action_confirm()
        cls.order = order
        cls.work_order = order.openmes_work_order_ids
        cls.work_order.write({"state": "sent", "mes_id": 42, "mes_status": "PENDING", "mes_planned_qty": 24})

    def _row(self, **vals):
        return dict({
            "id": 42, "order_no": self.work_order.name, "status": "IN_PROGRESS", "planned_qty": 24.0,
            "produced_qty": 5.0, "updated_at": "2026-11-06T09:15:01+00:00",
        }, **vals)

    # ── Receiving ────────────────────────────────────────────────────────────

    def test_receive_stores_the_event_without_processing_it(self):
        triggers = self._triggers(self.queue_cron)
        posts = len(self.order.message_ids)
        with self.mock_openmes():
            result = self._deliver("work_order.status_changed", self._row(), "q-1")
        self.assertEqual(result, (200, {"status": "queued"}))
        event = self._event("q-1")
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.payload, self._row())
        self.assertEqual(event.company_id, self.env.company)
        self.assertEqual(event.event, "work_order.status_changed")
        self.assertEqual(event.order_no, self.work_order.name)
        self.assertEqual(event.attempt_count, 0)
        self.env.invalidate_all()
        self.assertEqual(self.work_order.mes_status, "PENDING")
        self.assertEqual(len(self.order.message_ids), posts)
        self.assertEqual(self._triggers(self.queue_cron), triggers + 1)
        self.assertIn('"status": "IN_PROGRESS"', event.payload_text)

    def test_cron_processes_and_marks_done(self):
        self._deliver("work_order.status_changed", self._row(), "q-2")
        with self.mock_openmes():
            self._process()
        event = self._event("q-2")
        self.assertEqual((event.state, event.result, event.attempt_count), ("done", "processed", 0))
        self.assertTrue(event.processed_at)
        self.assertEqual((self.work_order.mes_status, self.work_order.produced_qty), ("IN_PROGRESS", 5))
        message = self.order.message_ids.filtered(lambda m: self.work_order.name in (m.body or ""))[:1]
        self.assertEqual(message.author_id, self.env.ref("base.partner_root"))

    def test_events_are_applied_in_arrival_order(self):
        self._deliver("work_order.status_changed", self._row(status="IN_PROGRESS", produced_qty=3,
                                                             updated_at="2026-11-06T09:00:00+00:00"), "o-1")
        self._deliver("work_order.status_changed", self._row(status="PAUSED", produced_qty=4,
                                                             updated_at="2026-11-06T09:05:00+00:00"), "o-2")
        applied = []
        original = OpenmesWorkOrder._openmes_apply_status

        def spy(records, row):
            applied.append(row["status"])
            return original(records, row)

        with patch.object(OpenmesWorkOrder, "_openmes_apply_status", spy):
            self._process()
        self.assertEqual(applied, ["IN_PROGRESS", "PAUSED"])
        self.assertEqual((self.work_order.mes_status, self.work_order.produced_qty), ("PAUSED", 4))

    def test_repeated_delivery_is_queued_once(self):
        self.assertEqual(self._deliver("work_order.progress", self._row(), "r-1"), (200, {"status": "queued"}))
        self.assertEqual(self._deliver("work_order.progress", self._row(produced_qty=9), "r-1"),
                         (200, {"status": "duplicate"}))
        # Still a duplicate once processed: done events are the replay ledger.
        self._process()
        self.assertEqual(self._deliver("work_order.progress", self._row(produced_qty=9), "r-1"),
                         (200, {"status": "duplicate"}))
        # A copy that passed the "seen" check at the same moment: the constraint stops it.
        with patch.object(OpenmesWebhook, "_openmes_seen", return_value=False), mute_logger("odoo.sql_db"):
            self.assertEqual(self._deliver("work_order.progress", self._row(produced_qty=9), "r-1"),
                             (200, {"status": "duplicate"}))
        self.assertEqual(len(self._event("r-1")), 1)
        self.assertEqual(self.work_order.produced_qty, 5)

    # ── Concurrency errors ───────────────────────────────────────────────────

    def _assert_transient_then_done(self, patcher):
        self._deliver("work_order.status_changed", self._row(), "t-1")
        self._deliver("work_order.progress", self._row(produced_qty=8, updated_at="2026-11-06T09:20:00+00:00"),
                      "t-2")
        with patcher:
            self._process()
        first, second = self._event("t-1"), self._event("t-2")
        self.assertEqual((first.state, first.attempt_count, first.last_error), ("pending", 0, False))
        self.assertEqual(second.state, "pending", "processing stops so the next event cannot overtake")
        self.assertEqual(self.work_order.mes_status, "PENDING")
        self.assertTrue(self._triggers(self.queue_cron, later_than=timedelta(seconds=20)))
        self._process()
        self.assertEqual((first.state, second.state), ("done", "done"))
        self.assertEqual((self.work_order.mes_status, self.work_order.produced_qty), ("IN_PROGRESS", 8))

    def test_serialization_failure_leaves_the_event_pending(self):
        self._assert_transient_then_done(
            self._fail_once(OpenmesWorkOrder, "_openmes_apply_status", _serialization_failure())
        )

    def test_lock_not_available_leaves_the_event_pending(self):
        self._assert_transient_then_done(
            self._fail_once(OpenmesWorkOrder, "_openmes_apply_status",
                            psycopg2.errors.LockNotAvailable("could not obtain lock"))
        )

    def test_busy_work_order_leaves_the_event_pending(self):
        """A send holds the work order's row: the event waits for it."""
        busy = patch.object(OpenmesWorkOrder, "_openmes_try_lock", lambda records: records.browse())
        self._assert_transient_then_done(busy)

    # ── Business errors ──────────────────────────────────────────────────────

    def test_business_error_is_retried_then_kept_in_error(self):
        self._deliver("work_order.status_changed", self._row(), "b-1")
        event = self._event("b-1")
        with patch.object(OpenmesWebhook, "_openmes_dispatch", side_effect=UserError("boom")):
            for attempt in range(1, MAX_EVENT_ATTEMPTS):
                self._process()
                self.assertEqual((event.state, event.attempt_count, event.last_error), ("pending", attempt, "boom"))
                self.assertTrue(event.next_attempt_at)
                # Not tried again before its time.
                self._process()
                self.assertEqual(event.attempt_count, attempt)
                event.write({"next_attempt_at": fields.Datetime.now() - timedelta(seconds=1)})
            self._process()
        self.assertEqual((event.state, event.attempt_count, event.last_error), ("error", MAX_EVENT_ATTEMPTS, "boom"))
        self.assertFalse(event.next_attempt_at)
        # Shown by the "Errors" filter, never picked up again by the cron.
        errors = self.env["openmes.webhook.event"].search([("state", "=", "error")])
        self.assertIn(event, errors)
        self._process()
        self.assertEqual(event.state, "error")
        self.assertEqual(self.work_order.mes_status, "PENDING")

    def test_business_error_does_not_block_the_next_event(self):
        self._deliver("quality_issue.created", {"work_order_no": self.work_order.name, "title": "x"}, "n-1")
        self._deliver("work_order.status_changed", self._row(), "n-2")
        with patch.object(OpenmesWebhook, "_openmes_note_quality_issue", side_effect=UserError("boom")):
            self._process()
        self.assertEqual(self._event("n-1").state, "pending")
        self.assertEqual(self._event("n-2").state, "done")
        self.assertTrue(self._triggers(self.queue_cron, later_than=timedelta(minutes=4)),
                        "the cron comes back for the retry")

    def test_invalid_values_go_to_error_at_once(self):
        for delivery, row in (("v-1", self._row(produced_qty="x")), ("v-2", self._row(id="x"))):
            with self.subTest(delivery=delivery):
                self.assertEqual(self._deliver("work_order.status_changed", row, delivery),
                                 (200, {"status": "queued"}))
                with mute_logger(WEBHOOK_LOGGER):
                    self._process()
                event = self._event(delivery)
                self.assertEqual((event.state, event.last_error), ("error", "The event carried invalid values."))
        self.assertEqual(self.work_order.mes_status, "PENDING")

    def test_unexpected_error_never_logs_the_payload(self):
        self._deliver("work_order.status_changed", self._row(customer="Secret Bakery Ltd"), "p-1")
        with patch.object(OpenmesWebhook, "_openmes_dispatch", side_effect=RuntimeError("bug")), \
                self.assertLogs(WEBHOOK_LOGGER, level="ERROR") as logs:
            self._process()
        output = "\n".join(logs.output)
        self.assertNotIn("Secret Bakery", output)
        self.assertNotIn(self.work_order.name, output)
        event = self._event("p-1")
        self.assertEqual((event.state, event.attempt_count), ("pending", 1))
        self.assertEqual(event.last_error, "Unexpected error; see the server log.")

    # ── Disabled company ─────────────────────────────────────────────────────

    def test_disabled_company_at_receive_time_stores_nothing(self):
        self._configure(enabled=False)
        self.assertEqual(self._deliver("work_order.status_changed", self._row(), "x-1"),
                         (200, {"status": "ignored"}))
        self.assertFalse(self._event("x-1"))

    def test_disabled_after_queueing_applies_nothing(self):
        self._deliver("work_order.status_changed", self._row(), "x-2")
        self._configure(enabled=False)
        self._process()
        event = self._event("x-2")
        self.assertEqual((event.state, event.result), ("done", "ignored"))
        self.assertEqual(self.work_order.mes_status, "PENDING")

    # ── Access ───────────────────────────────────────────────────────────────

    def test_access_rights(self):
        self._deliver("work_order.status_changed", self._row(), "a-1")
        event = self._event("a-1")
        event.write({"state": "error", "attempt_count": MAX_EVENT_ATTEMPTS, "last_error": "boom"})
        Event = self.env["openmes.webhook.event"]
        salesman = new_test_user(self.env, "om_q_salesman", groups="sales_team.group_sale_salesman")
        mrp_user = new_test_user(self.env, "om_q_mrp_user", groups="mrp.group_mrp_user")
        sale_manager = new_test_user(self.env, "om_q_sale_manager", groups="sales_team.group_sale_manager")
        mrp_manager = new_test_user(self.env, "om_q_mrp_manager", groups="mrp.group_mrp_manager")
        admin = new_test_user(self.env, "om_q_admin", groups="base.group_system")
        for user in (salesman, mrp_user):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                Event.with_user(user).search([])
        for user in (sale_manager, mrp_manager):
            with self.subTest(user=user.login):
                self.assertEqual(Event.with_user(user).search([("delivery_id", "=", "a-1")]), event)
                with self.assertRaises(AccessError):
                    event.with_user(user).action_retry()
                with self.assertRaises(AccessError):
                    event.with_user(user).write({"state": "pending"})
                with self.assertRaises(AccessError):
                    Event.with_user(user).create({"delivery_id": "forged", "event": "work_order.progress",
                                                  "company_id": self.env.company.id})
        menus = {
            sale_manager: "openmes_connector.menu_openmes_webhook_events",
            mrp_manager: "openmes_connector.menu_openmes_webhook_events_mrp",
        }
        for user, xmlid in menus.items():
            visible = self.env["ir.ui.menu"].with_user(user)._visible_menu_ids()
            self.assertIn(self.env.ref(xmlid).id, visible)
        hidden = self.env["ir.ui.menu"].with_user(salesman)._visible_menu_ids()
        self.assertNotIn(self.env.ref("openmes_connector.menu_openmes_webhook_events").id, hidden)
        # The administrator retries: back to pending, attempts reset, cron triggered.
        triggers = self._triggers(self.queue_cron)
        event.with_user(admin).action_retry()
        self.assertEqual((event.state, event.attempt_count, event.last_error), ("pending", 0, False))
        self.assertEqual(self._triggers(self.queue_cron), triggers + 1)
        with self.mock_openmes():
            self._process()
        self.assertEqual(event.state, "done")

    # ── Housekeeping ─────────────────────────────────────────────────────────

    def test_migration_keeps_the_recent_replay_ledger(self):
        cr = self.env.cr
        # The 18.0.1.1.8 table; DDL is rolled back with the test.
        cr.execute("""
            CREATE TABLE openmes_webhook_delivery (
                id serial PRIMARY KEY, delivery_id varchar NOT NULL, event varchar,
                company_id integer, received_at timestamp NOT NULL
            )
        """)
        cr.execute("""
            INSERT INTO openmes_webhook_delivery (delivery_id, event, company_id, received_at) VALUES
                ('m-recent', 'work_order.progress', %(company)s, (now() AT TIME ZONE 'UTC') - interval '1 hour'),
                ('m-old', 'work_order.progress', %(company)s, (now() AT TIME ZONE 'UTC') - interval '30 hours'),
                ('m-none', NULL, NULL, (now() AT TIME ZONE 'UTC') - interval '1 hour')
        """, {"company": self.env.company.id})
        _load("18.0.1.1.9", "post-migrate").migrate(cr, "18.0.1.1.8")
        self.env.invalidate_all()
        recent = self._event("m-recent")
        self.assertEqual((recent.state, recent.result, recent.company_id), ("done", "processed", self.env.company))
        self.assertFalse(self._event("m-old"))
        self.assertFalse(self._event("m-none"))
        # Replay protection survived the upgrade.
        self.assertEqual(self._deliver("work_order.progress", self._row(), "m-recent"),
                         (200, {"status": "duplicate"}))
        # Run twice (or on a fresh install): nothing breaks.
        _load("18.0.1.1.9", "post-migrate").migrate(cr, "18.0.1.1.8")
        _load("18.0.1.1.9", "post-migrate").migrate(cr, None)
        self.assertEqual(len(self._event("m-recent")), 1)


@tagged("post_install", "-at_install")
class TestSendRace(QueueCase):
    """Manufacturing-order mode: an under-produced DONE closes the MO, the
    backorder gets its own work order, and its send meets the
    ``work_order.created`` event OpenMES sends for it."""

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

    def _production(self):
        production = self.env["mrp.production"].create({
            "product_id": self.bun.id, "product_qty": 10, "product_uom_id": self.bun.uom_id.id,
        })
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS", "mes_planned_qty": 10})
        return production, work_order

    def _close_with_backorder(self):
        """Queue and process the under-produced DONE; returns the backorder's work order."""
        production, work_order = self._production()
        triggers = self._triggers(self.send_cron)
        self._deliver("work_order.status_changed", {
            "id": 77, "order_no": work_order.name, "status": "DONE", "planned_qty": 10.0, "produced_qty": 6.0,
            "completed_at": "2026-11-06T09:15:01+00:00", "updated_at": "2026-11-06T09:15:01+00:00",
        }, "bo-done")
        with self.mock_openmes():
            self._process()
        self.assertEqual(self._event("bo-done").state, "done")
        self.assertEqual(production.state, "done")
        backorder = production.procurement_group_id.mrp_production_ids - production
        self.assertEqual(len(backorder), 1)
        backorder_wo = backorder.openmes_work_order_ids
        self.assertEqual(backorder_wo.state, "to_send")
        self.assertGreater(self._triggers(self.send_cron), triggers, "the send cron is triggered")
        return backorder_wo

    def _created_event(self, work_order, delivery="bo-created"):
        self._deliver("work_order.created", {
            "id": 78, "order_no": work_order.name, "status": "PENDING", "planned_qty": 4.0, "produced_qty": 0,
            "updated_at": "2026-11-06T09:15:05+00:00",
        }, delivery)
        with self.mock_openmes():
            self._process()
        self.assertEqual(self._event(delivery).state, "done")

    def _created_report(self):
        return import_report(imported=1, results=[{"row": 1, "action": "created", "id": 78}])

    def test_backorder_work_order_ends_sent(self):
        backorder_wo = self._close_with_backorder()
        self._created_event(backorder_wo)
        self.assertEqual((backorder_wo.mes_id, backorder_wo.state), (78, "to_send"))
        with self.mock_openmes(self._created_report()) as calls:
            self.env["openmes.work.order"]._cron_openmes_send()
        self.assertEqual([o["order_no"] for o in calls[0]["json"]["orders"]], [backorder_wo.name])
        self.env.invalidate_all()
        self.assertEqual((backorder_wo.state, backorder_wo.mes_id, backorder_wo.attempt_count), ("sent", 78, 0))

    def test_conflict_when_saving_the_result_keeps_the_order_to_send(self):
        backorder_wo = self._close_with_backorder()
        triggers = self._triggers(self.send_cron, later_than=timedelta(seconds=30))
        failing = self._fail_once(OpenmesWorkOrder, "_openmes_apply_result", _serialization_failure())
        with failing, self.mock_openmes(self._created_report()):
            self.env["openmes.work.order"]._cron_openmes_send()
        self.env.invalidate_all()
        self.assertEqual((backorder_wo.state, backorder_wo.attempt_count, backorder_wo.last_error),
                         ("to_send", 0, False))
        self.assertGreater(self._triggers(self.send_cron, later_than=timedelta(seconds=30)), triggers,
                           "sent again within a minute")
        # The event for the new order meanwhile, then the resend (update_or_create).
        self._created_event(backorder_wo)
        with self.mock_openmes(import_report(updated=1, results=[{"row": 1, "action": "updated", "id": 78}])):
            self.env["openmes.work.order"]._cron_openmes_send()
        self.env.invalidate_all()
        self.assertEqual((backorder_wo.state, backorder_wo.mes_id), ("sent", 78))

    def test_closing_the_mo_reraises_concurrency_errors(self):
        production, work_order = self._production()
        self._deliver("work_order.status_changed", {
            "id": 77, "order_no": work_order.name, "status": "DONE", "planned_qty": 10.0, "produced_qty": 10.0,
            "updated_at": "2026-11-06T09:15:01+00:00",
        }, "mo-1")
        posts = len(production.message_ids)
        Production = type(self.env["mrp.production"])
        with patch.object(Production, "button_mark_done", side_effect=_serialization_failure()):
            self._process()
        event = self._event("mo-1")
        self.assertEqual((event.state, event.attempt_count), ("pending", 0))
        self.assertFalse(work_order.last_error, "not reported as a failed close")
        self.assertEqual(work_order.mes_status, "IN_PROGRESS", "the whole event was rolled back")
        self.assertEqual(len(production.message_ids), posts)
        self.assertNotEqual(production.state, "done")
        self._process()
        self.assertEqual(event.state, "done")
        self.assertEqual(production.state, "done")

    def test_busy_work_order_is_not_sent(self):
        production, work_order = self._production()
        work_order.write({"state": "to_send"})
        triggers = self._triggers(self.send_cron, later_than=timedelta(seconds=30))
        with patch.object(OpenmesWorkOrder, "_openmes_try_lock", lambda records: records.browse()), \
                self.mock_openmes():  # no HTTP call
            busy = work_order._openmes_send()
            self.assertEqual(busy, work_order)
            self.assertEqual((work_order.state, work_order.attempt_count), ("to_send", 0))
            action = work_order.action_send()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertIn("being updated right now", action["params"]["message"])
        self.assertGreater(self._triggers(self.send_cron, later_than=timedelta(seconds=30)), triggers)

    def test_server_error_after_the_lock_marks_the_order_in_error(self):
        production, work_order = self._production()
        work_order.write({"state": "to_send"})
        with self.mock_openmes(FakeResponse(500, None)):
            busy = work_order._openmes_send()
        self.assertFalse(busy)
        self.assertEqual((work_order.state, work_order.attempt_count), ("error", 1))
        self.assertTrue(work_order.last_error)

    def test_busy_cancellation_is_not_sent(self):
        production, work_order = self._production()
        work_order.write({"pending_cancel": True})
        with patch.object(OpenmesWorkOrder, "_openmes_try_lock", lambda records: records.browse()), \
                self.mock_openmes():
            busy = work_order._openmes_send_cancellations()
        self.assertEqual(busy, work_order)
        self.assertTrue(work_order.pending_cancel)
