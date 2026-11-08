import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger

from .common import FakeResponse, OpenmesCase

WEBHOOK_URL = "/openmes/webhook"
SECRET = "Zx8" + "s" * 37
CONTROLLER_LOGGER = "odoo.addons.openmes_connector.controllers.webhook"


@tagged("post_install", "-at_install")
class TestWebhook(OpenmesCase, HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._configure(webhook_id=12, webhook_secret=SECRET)
        order = cls.env["sale.order"].create({
            "partner_id": cls.partner.id,
            "order_line": [(0, 0, {"product_id": cls.bread.id, "product_uom_qty": 24})],
        })
        order.action_confirm()
        cls.order = order
        cls.work_order = order.openmes_work_order_ids
        cls.work_order.write({"state": "sent", "mes_id": 0, "mes_status": "PENDING", "mes_planned_qty": 24})

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _row(self, **vals):
        return dict({
            "id": 42, "order_no": self.work_order.name, "status": "IN_PROGRESS", "planned_qty": 24.0,
            "produced_qty": 5.0, "completed_at": None, "updated_at": "2026-11-06T09:15:01+00:00",
            "previous_status": "PENDING",
        }, **vals)

    @staticmethod
    def _sign(raw, ts, secret=SECRET):
        return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()

    def _headers(self, event, raw, delivery=None, ts=None, secret=SECRET):
        ts = str(int(time.time())) if ts is None else str(ts)
        return {
            "Content-Type": "application/json",
            "X-OpenMES-Event": event,
            "X-OpenMES-Delivery": delivery or str(uuid.uuid4()),
            "X-OpenMES-Timestamp": ts,
            "X-OpenMES-Signature": self._sign(raw, ts, secret),
        }

    @staticmethod
    def _body(event, data):
        return json.dumps({"event": event, "data": data, "timestamp": "2026-11-06T09:15:02+00:00"}).encode()

    def _post(self, event, data, raw=None, drop=(), override=None, **header_kw):
        raw = raw if raw is not None else self._body(event, data)
        headers = self._headers(event, raw, **header_kw)
        for name in drop:
            headers.pop(name)
        headers.update(override or {})
        return self.url_open(WEBHOOK_URL, data=raw, headers=headers)

    def _snapshot(self):
        self.env.invalidate_all()
        wo = self.work_order
        return (wo.mes_status, wo.produced_qty, wo.mes_id, wo.mes_updated_at, len(wo.message_ids),
                len(self.order.message_ids), self.env["openmes.webhook.event"].search_count([]))

    def _process(self):
        self.env["openmes.webhook.event"]._cron_openmes_process_events()
        self.env.invalidate_all()

    def assertRejected(self, response, before):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.content, b"")
        self.assertEqual(self._snapshot(), before)

    # ── Accepted deliveries ──────────────────────────────────────────────────

    def test_status_changed_updates_the_work_order(self):
        with self.mock_openmes():  # no call to OpenMES
            response = self._post("work_order.status_changed", self._row(), delivery="d-1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "queued"})
            self._process()
        wo = self.work_order
        self.assertEqual((wo.mes_status, wo.produced_qty, wo.mes_id), ("IN_PROGRESS", 5, 42))
        self.assertEqual(str(wo.mes_updated_at), "2026-11-06 09:15:01")
        message = self.order.message_ids.filtered(lambda m: wo.name in (m.body or ""))[:1]
        self.assertTrue(message)
        # Processed as superuser: the author is OdooBot, never the public user.
        self.assertEqual(message.author_id, self.env.ref("base.partner_root"))
        event = self.env["openmes.webhook.event"].search([("delivery_id", "=", "d-1")])
        self.assertEqual((event.event, event.state, event.result), ("work_order.status_changed", "done", "processed"))
        # The pull cursor belongs to the pull.
        self.assertFalse(self.env.company.openmes_status_since)

    def test_progress_and_created_events(self):
        response = self._post("work_order.created", self._row(status="PENDING", produced_qty=0,
                                                              updated_at="2026-11-06T09:00:00+00:00"))
        self.assertEqual(response.status_code, 200)
        self._process()
        self.assertEqual(self.work_order.mes_id, 42)
        response = self._post("work_order.progress", self._row(status="PENDING", produced_qty=7.5,
                                                               previous_produced_qty=0))
        self.assertEqual(response.status_code, 200)
        self._process()
        self.assertEqual((self.work_order.mes_status, self.work_order.produced_qty), ("PENDING", 7.5))

    def test_unknown_order_number_is_ignored(self):
        before = self._snapshot()
        response = self._post("work_order.status_changed", self._row(order_no="FOREIGN-1"), delivery="d-f")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "queued"})
        self._process()
        self.assertEqual(self._snapshot()[:6], before[:6])
        event = self.env["openmes.webhook.event"].search([("delivery_id", "=", "d-f")])
        self.assertEqual((event.state, event.result, event.order_no), ("done", "ignored", "FOREIGN-1"))

    def test_unknown_and_test_events_are_ignored(self):
        for event in ("batch.completed", "webhook.test", "something.new"):
            with self.subTest(event=event), self.mock_openmes():
                response = self._post(event, {"id": 1, "order_no": self.work_order.name, "status": "DONE"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ignored"})
        self.env.invalidate_all()
        self.assertEqual(self.work_order.mes_status, "PENDING")
        # Not stored: the event list is about work to do.
        self.assertFalse(self.env["openmes.webhook.event"].search_count([]))

    def test_stock_document_posted_triggers_the_booking_cron(self):
        cron = self.env.ref("openmes_connector.ir_cron_openmes_stock_documents")
        Trigger = self.env["ir.cron.trigger"].sudo()
        before = Trigger.search_count([("cron_id", "=", cron.id)])
        data = {"id": 9, "document_no": "PR/2026/0009", "type": "product_receipt", "status": "posted",
                "lines": [{"product_type_code": "BREAD-01", "quantity": 100.0}]}
        with self.mock_openmes():  # the cron fetches the documents itself, later
            response = self._post("stock_document.posted", data)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "queued"})
            self.assertEqual(Trigger.search_count([("cron_id", "=", cron.id)]), before)
            self._process()
        self.assertEqual(Trigger.search_count([("cron_id", "=", cron.id)]), before + 1)
        self.assertFalse(self.env["openmes.stock.document"].sudo().search_count([]))

    def test_quality_issue_posts_an_escaped_note(self):
        data = {"id": 17, "title": "<b>Seal</b> not closed", "severity": "HIGH", "issue_type": "Seal defect",
                "non_conforming_qty": 3.0, "work_order_no": self.work_order.name}
        response = self._post("quality_issue.created", data)
        self.assertEqual(response.status_code, 200)
        self._process()
        bodies = " ".join(m.body or "" for m in self.work_order.message_ids)
        self.assertIn("&lt;b&gt;Seal&lt;/b&gt; not closed", bodies)
        self.assertNotIn("<b>Seal</b>", bodies)
        self.assertIn("HIGH", bodies)
        # Not for an unknown work order.
        response = self._post("quality_issue.created", dict(data, work_order_no="FOREIGN-1"), delivery="q-2")
        self.assertEqual(response.json(), {"status": "queued"})
        self._process()
        event = self.env["openmes.webhook.event"].search([("delivery_id", "=", "q-2")])
        self.assertEqual((event.state, event.result, event.order_no), ("done", "ignored", "FOREIGN-1"))

    def test_disabled_integration_ignores_the_event(self):
        self._configure(enabled=False)
        before = self._snapshot()
        response = self._post("work_order.status_changed", self._row())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})
        self._process()
        self.assertEqual(self._snapshot(), before)

    def test_older_state_never_overwrites_a_newer_one(self):
        self._post("work_order.status_changed",
                   self._row(status="DONE", produced_qty=24, completed_at="2026-11-06T10:00:00+00:00",
                             updated_at="2026-11-06T10:00:00+00:00"))
        self._process()
        # A late retry of an earlier event.
        response = self._post("work_order.progress", self._row(status="IN_PROGRESS", produced_qty=20,
                                                               updated_at="2026-11-06T09:59:00+00:00"))
        self.assertEqual(response.status_code, 200)
        self._process()
        self.assertEqual((self.work_order.mes_status, self.work_order.produced_qty), ("DONE", 24))

    def test_pull_does_not_roll_back_a_newer_webhook_state(self):
        self._post("work_order.status_changed", self._row(status="IN_PROGRESS", produced_qty=12,
                                                          updated_at="2026-11-06T10:00:00+00:00"))
        self._process()
        page = FakeResponse(200, {"data": [self._row(status="IN_PROGRESS", produced_qty=3,
                                                     updated_at="2026-11-06T09:00:00+00:00")],
                                  "meta": {"next_cursor": None}})
        with self.mock_openmes(page):
            self.env["openmes.work.order"]._cron_openmes_pull_status()
        self.assertEqual(self.work_order.produced_qty, 12)

    # ── Rejected deliveries ──────────────────────────────────────────────────

    def test_wrong_signature_is_rejected(self):
        before = self._snapshot()
        raw = self._body("work_order.status_changed", self._row())
        legacy = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        cases = {
            "wrong secret": {"secret": "another-secret-of-40-characters-xxxxxxxx"},
            "garbage": {"override": {"X-OpenMES-Signature": "sha256=deadbeef"}},
            "legacy body-only": {"override": {"X-OpenMES-Signature": legacy}},
            "tampered body": {"raw": raw.replace(b"IN_PROGRESS", b"DONE")},
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                if "raw" in kwargs:
                    headers = self._headers("work_order.status_changed", raw)
                    response = self.url_open(WEBHOOK_URL, data=kwargs["raw"], headers=headers)
                else:
                    response = self._post("work_order.status_changed", self._row(), raw=raw, **kwargs)
                self.assertRejected(response, before)

    def test_missing_headers_are_rejected(self):
        before = self._snapshot()
        for header in ("X-OpenMES-Event", "X-OpenMES-Delivery", "X-OpenMES-Timestamp", "X-OpenMES-Signature"):
            with self.subTest(header=header):
                self.assertRejected(self._post("work_order.status_changed", self._row(), drop=(header,)), before)

    def test_stale_or_malformed_timestamp_is_rejected(self):
        before = self._snapshot()
        now = int(time.time())
        for ts in (now - 400, now + 400, "abc", "-5"):
            with self.subTest(ts=ts):
                self.assertRejected(self._post("work_order.status_changed", self._row(), ts=ts), before)

    def test_repeated_delivery_is_acknowledged_without_changes(self):
        response = self._post("work_order.status_changed", self._row(), delivery="d-42")
        self.assertEqual(response.status_code, 200)
        self._process()
        before = self._snapshot()
        # Fresh timestamp, valid signature, other payload: still the same delivery.
        # OpenMES retries when it missed the first answer; a 401 would show it as failed.
        response = self._post("work_order.status_changed",
                              self._row(status="DONE", produced_qty=24, updated_at="2026-11-06T11:00:00+00:00"),
                              delivery="d-42")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "duplicate"})
        self._process()
        self.assertEqual(self._snapshot(), before)

    def test_repeated_delivery_id_with_a_bad_signature_is_rejected(self):
        self.assertEqual(self._post("work_order.status_changed", self._row(), delivery="d-43").status_code, 200)
        before = self._snapshot()
        response = self._post("work_order.status_changed", self._row(status="DONE"), delivery="d-43",
                              secret="Bad" + "b" * 37)
        self.assertRejected(response, before)

    def test_concurrent_copy_is_acknowledged_once(self):
        """Two copies at the same moment: both pass the "seen" check, the
        unique constraint stops the second one, which is answered 200."""
        Webhook = self.env["openmes.webhook"]
        raw = self._body("work_order.status_changed", self._row(status="IN_PROGRESS", produced_qty=5))
        headers = self._headers("work_order.status_changed", raw, delivery="d-44")
        self.assertEqual(Webhook._openmes_receive(headers, raw), (200, {"status": "queued"}))
        self._process()
        posts = len(self.order.message_ids)
        raw = self._body("work_order.status_changed",
                         self._row(status="DONE", produced_qty=24, updated_at="2026-11-06T12:00:00+00:00"))
        headers = self._headers("work_order.status_changed", raw, delivery="d-44")
        with patch.object(type(Webhook), "_openmes_seen", return_value=False), mute_logger("odoo.sql_db"):
            self.assertEqual(Webhook._openmes_receive(headers, raw), (200, {"status": "duplicate"}))
        self._process()
        self.assertEqual((self.work_order.mes_status, self.work_order.produced_qty), ("IN_PROGRESS", 5))
        self.assertEqual(len(self.order.message_ids), posts)
        self.assertEqual(self.env["openmes.webhook.event"].search_count([("delivery_id", "=", "d-44")]), 1)

    def test_no_registered_secret_rejects_everything(self):
        self._configure(webhook_secret=False)
        before = self._snapshot()
        self.assertRejected(self._post("work_order.status_changed", self._row()), before)

    def test_event_header_must_match_the_signed_body(self):
        before = self._snapshot()
        raw = self._body("work_order.status_changed", self._row())
        response = self._post("webhook.test", None, raw=raw)
        self.assertRejected(response, before)

    def test_malformed_delivery_id_is_rejected(self):
        before = self._snapshot()
        self.assertRejected(self._post("work_order.status_changed", self._row(), delivery="a b/../c"), before)

    def test_oversized_body_and_get_are_refused(self):
        before = self._snapshot()
        data = self._row(padding="x" * (1024 * 1024))
        response = self._post("work_order.status_changed", data)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self._snapshot(), before)
        response = self.url_open(WEBHOOK_URL)
        self.assertNotEqual(response.status_code, 200)

    def test_malformed_signed_payload_is_a_bad_request(self):
        before = self._snapshot()
        event = "work_order.status_changed"
        for name, raw in {
            "not json": b"{not json",
            "a list": b"[1, 2]",
            "data not a dict": json.dumps({"event": event, "data": [1]}).encode(),
        }.items():
            with self.subTest(case=name):
                response = self._post(event, None, raw=raw)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self._snapshot(), before)

    def test_rejections_never_log_the_secret_or_signature(self):
        raw = self._body("work_order.status_changed", self._row())
        headers = self._headers("work_order.status_changed", raw, secret="another-secret-of-40-characters-xxxxxxxx")
        with self.assertLogs(CONTROLLER_LOGGER, level="WARNING") as logs:
            self.url_open(WEBHOOK_URL, data=raw, headers=headers)
        output = "\n".join(logs.output)
        self.assertIn("rejected", output)
        self.assertNotIn(SECRET, output)
        self.assertNotIn(headers["X-OpenMES-Signature"], output)
        self.assertNotIn(headers["X-OpenMES-Signature"][7:], output)

    # ── Housekeeping ─────────────────────────────────────────────────────────

    def test_autovacuum_forgets_processed_events_after_a_week(self):
        Event = self.env["openmes.webhook.event"].sudo()
        now = fields.Datetime.now()

        def event(name, state, age):
            return Event.create({"delivery_id": name, "event": "work_order.progress", "state": state,
                                 "company_id": self.env.company.id, "received_at": now - age})

        old = event("old", "done", timedelta(days=8))
        recent = event("recent", "done", timedelta(days=1))
        pending = event("pending", "pending", timedelta(days=30))
        failed = event("failed", "error", timedelta(days=30))
        Event._gc_openmes_events()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(pending.exists())
        self.assertTrue(failed.exists())


@tagged("post_install", "-at_install")
class TestWebhookManufacturingOrder(OpenmesCase, HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._configure(order_source="manufacturing_orders", webhook_id=12, webhook_secret=SECRET)
        cls.bun = cls.env["product.product"].create({
            "name": "Bun", "default_code": "BUN-01", "is_storable": True, "openmes_manufacture": True,
        })
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.bun.product_tmpl_id.id,
            "product_qty": 10,
            "bom_line_ids": [(0, 0, {"product_id": cls.flour.id, "product_qty": 1})],
        })

    def test_done_event_closes_the_manufacturing_order(self):
        production = self.env["mrp.production"].create({
            # Odoo 20: mrp.bom(.line), mrp.production and mrp.workcenter.capacity renamed product_uom_id to uom_id.
            "product_id": self.bun.id, "product_qty": 10, "uom_id": self.bun.uom_id.id,
        })
        production.action_confirm()
        work_order = production.openmes_work_order_ids
        work_order.write({"state": "sent", "mes_id": 77, "mes_status": "IN_PROGRESS"})
        raw = TestWebhook._body("work_order.status_changed", {
            "id": 77, "order_no": work_order.name, "status": "DONE", "planned_qty": 10.0, "produced_qty": 10.0,
            "completed_at": "2026-11-06T09:15:01+00:00", "updated_at": "2026-11-06T09:15:01+00:00",
        })
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-OpenMES-Event": "work_order.status_changed",
            "X-OpenMES-Delivery": "mo-done-1",
            "X-OpenMES-Timestamp": ts,
            "X-OpenMES-Signature": TestWebhook._sign(raw, ts),
        }
        response = self.url_open(WEBHOOK_URL, data=raw, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "queued"})
        self.env.invalidate_all()
        self.assertNotEqual(production.state, "done", "the request only queues the event")
        self.env["openmes.webhook.event"]._cron_openmes_process_events()
        self.env.invalidate_all()
        self.assertEqual(work_order.mes_status, "DONE")
        self.assertEqual(production.state, "done")
