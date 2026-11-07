"""Receiver side of the OpenMES webhooks (``POST /openmes/webhook``).

The controller only moves bytes; everything here is private so it can never be
reached through RPC. A delivery is trusted only after its HMAC signature, its
timestamp and its delivery id have been checked, and even then the payload is
data: values are read and converted, nothing in it is ever executed.
"""

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import timedelta

import psycopg2

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_round

from .openmes_client import CONCURRENCY_ERRORS, WEBHOOK_TOLERANCE

_logger = logging.getLogger(__name__)

DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
WORK_ORDER_EVENTS = ("work_order.created", "work_order.status_changed", "work_order.progress")
# Events this module acts on; every other one is acknowledged and dropped.
HANDLED_EVENTS = WORK_ORDER_EVENTS + ("stock_document.posted", "quality_issue.created")

# Processed events are the replay ledger: kept well beyond OpenMES' retries
# and the 5-minute timestamp window (at least 24 h).
EVENT_DONE_TTL = timedelta(days=7)
# A business error (UserError, a bug) is tried this many times, then the event
# stays in Error until an administrator retries it.
MAX_EVENT_ATTEMPTS = 5
# Wait between two tries of an event that failed with a business error.
EVENT_RETRY_DELAY = timedelta(minutes=5)
# Wait after a concurrency error (another transaction on the same rows).
TRANSIENT_RETRY_DELAY = timedelta(seconds=30)
EVENT_BATCH = 50

HEADER_EVENT = "X-OpenMES-Event"
HEADER_DELIVERY = "X-OpenMES-Delivery"
HEADER_TIMESTAMP = "X-OpenMES-Timestamp"
HEADER_SIGNATURE = "X-OpenMES-Signature"


class WebhookRejected(Exception):
    """The delivery is not authentic: answer 401, change nothing."""


class WorkOrderBusy(Exception):
    """The work order of an event is being changed by another transaction (a
    send in progress): process the event again a little later."""


class OpenmesWebhookEvent(models.Model):
    """One authentic webhook delivery, stored by the controller and applied by
    the "OpenMES: process webhook events" cron in arrival order.

    Only the controller (as superuser) creates events; managers read them,
    administrators retry the ones in error.
    """

    _name = "openmes.webhook.event"
    _description = "OpenMES Webhook Event"
    _order = "id desc"
    _rec_name = "delivery_id"

    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, index=True, ondelete="cascade"
    )
    event = fields.Char("Event", required=True, readonly=True)
    delivery_id = fields.Char("Delivery ID", required=True, readonly=True, index=True)
    order_no = fields.Char("OpenMES Order No.", readonly=True, index=True)
    payload = fields.Json("Raw Payload", readonly=True)
    payload_text = fields.Text("Payload", compute="_compute_payload_text")
    state = fields.Selection(
        [("pending", "Pending"), ("done", "Done"), ("error", "Error")],
        default="pending",
        required=True,
        readonly=True,
        index=True,
    )
    result = fields.Selection(
        [("processed", "Processed"), ("ignored", "Nothing to do")],
        readonly=True,
        help="Whether the event changed anything in Odoo (e.g. an unknown order number changes nothing).",
    )
    attempt_count = fields.Integer("Attempts", readonly=True)
    next_attempt_at = fields.Datetime("Next Attempt", readonly=True)
    last_error = fields.Text(readonly=True)
    received_at = fields.Datetime(required=True, readonly=True, default=fields.Datetime.now, index=True)
    processed_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        # Same name as before: delivery ids are unique within one OpenMES instance.
        (
            "delivery_id_unique",
            "unique(delivery_id, company_id)",
            "An OpenMES webhook delivery is processed only once.",
        ),
    ]

    @api.depends("payload")
    def _compute_payload_text(self):
        for record in self:
            record.payload_text = (
                json.dumps(record.payload, indent=2, ensure_ascii=False, sort_keys=True)
                if record.payload is not None
                else False
            )

    @api.autovacuum
    def _gc_openmes_events(self):
        """Processed events are deleted after EVENT_DONE_TTL; pending and
        failed ones stay until they are dealt with."""
        self.sudo().search(
            [("state", "=", "done"), ("received_at", "<", fields.Datetime.now() - EVENT_DONE_TTL)]
        ).unlink()

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_retry(self):
        """Queue failed events again (administrators). Never processed inline,
        so there is only ever one processor: the cron."""
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an administrator can retry OpenMES webhook events."))
        self.check_access("write")
        failed = self.filtered(lambda r: r.state == "error")
        failed.write({"state": "pending", "attempt_count": 0, "next_attempt_at": False, "last_error": False})
        if failed:
            self._openmes_cron()._trigger()

    # ── Processing ───────────────────────────────────────────────────────────

    @api.model
    def _openmes_cron(self):
        return self.env.ref("openmes_connector.ir_cron_openmes_webhook_events").sudo()

    @api.model
    def _openmes_due_domain(self):
        return [
            ("state", "=", "pending"),
            "|",
            ("next_attempt_at", "=", False),
            ("next_attempt_at", "<=", fields.Datetime.now()),
        ]

    @api.model
    def _cron_openmes_process_events(self):
        """Apply the pending events in arrival order, each one committed on its
        own: a slow or failing event never rolls back the ones before it."""
        Document = self.env["openmes.stock.document"]
        events = self.sudo().search(self._openmes_due_domain(), order="id", limit=EVENT_BATCH)
        done = 0
        for event in events:
            outcome = event._openmes_process()
            Document._openmes_commit()
            if outcome == "transient":
                # Stop here: the next events may be about the same order and
                # must not overtake this one.
                self._openmes_cron()._trigger(fields.Datetime.now() + TRANSIENT_RETRY_DELAY)
                return
            done += 1
        retry_at = self.sudo().search(
            [("state", "=", "pending"), ("next_attempt_at", "!=", False)], order="next_attempt_at", limit=1
        ).next_attempt_at
        if retry_at:
            self._openmes_cron()._trigger(retry_at)
        remaining = self.sudo().search_count(self._openmes_due_domain()) if len(events) == EVENT_BATCH else 0
        self.env["ir.cron"]._notify_progress(done=done, remaining=remaining)

    def _openmes_process(self):
        """Apply this event. Returns "transient" when it has to wait for
        another transaction (it stays pending, no attempt counted), else "ok"."""
        self.ensure_one()
        event = self.sudo()
        company = event.company_id
        now = fields.Datetime.now()
        if not company.openmes_enabled:
            # Disabled after the event was queued: nothing is applied any more.
            event.write({"state": "done", "result": "ignored", "processed_at": now, "last_error": False})
            return "ok"
        try:
            with self.env.cr.savepoint():
                handled = self.env["openmes.webhook"].with_company(company)._openmes_dispatch(
                    event.event, event.payload or {}, company
                )
                self.env.flush_all()
        except (*CONCURRENCY_ERRORS, WorkOrderBusy):
            self.env.invalidate_all()
            # Ids only: the payload may name a customer.
            _logger.info("OpenMES webhook event %s waits for another transaction; retried shortly", event.id)
            return "transient"
        except (ValueError, TypeError, OverflowError):
            # Signed but malformed values (e.g. a non-numeric quantity): the
            # same every time, so no point in trying again.
            self.env.invalidate_all()
            _logger.warning("OpenMES webhook event %s (%s) carried invalid values", event.id, event.event)
            event.write({
                "state": "error",
                "attempt_count": event.attempt_count + 1,
                "next_attempt_at": False,
                "last_error": _("The event carried invalid values."),
                "processed_at": now,
            })
            return "ok"
        except Exception as exc:
            self.env.invalidate_all()
            if isinstance(exc, (UserError, ValidationError)):
                message = str(exc.args[0]) if exc.args else str(exc)
            else:
                # Details (never the payload) go to the server log only.
                _logger.exception("OpenMES webhook event %s (%s) failed", event.id, event.event)
                message = _("Unexpected error; see the server log.")
            attempts = event.attempt_count + 1
            failed = attempts >= MAX_EVENT_ATTEMPTS
            event.write({
                "state": "error" if failed else "pending",
                "attempt_count": attempts,
                "next_attempt_at": False if failed else now + EVENT_RETRY_DELAY,
                "last_error": message,
                "processed_at": now if failed else False,
            })
            return "ok"
        event.write({
            "state": "done",
            "result": "processed" if handled else "ignored",
            "processed_at": now,
            "next_attempt_at": False,
            "last_error": False,
        })
        return "ok"


class OpenmesWebhook(models.AbstractModel):
    _name = "openmes.webhook"
    _description = "OpenMES Webhook Receiver"

    # ── Authentication ───────────────────────────────────────────────────────

    @api.model
    def _openmes_verify(self, headers, raw_body):
        """Raise WebhookRejected unless the delivery is authentic.
        Returns ``(event, delivery id, company)``: the company is the one whose
        webhook secret signed the delivery (one URL serves every company).

        ``headers`` is any case-insensitive mapping (werkzeug headers). The
        reason in the exception is for the server log only, never for the sender.
        """
        event = headers.get(HEADER_EVENT) or ""
        delivery = headers.get(HEADER_DELIVERY) or ""
        timestamp = headers.get(HEADER_TIMESTAMP) or ""
        signature = headers.get(HEADER_SIGNATURE) or ""
        if not (event and delivery and timestamp and signature):
            raise WebhookRejected("missing headers")
        if not DELIVERY_ID_RE.match(delivery):
            raise WebhookRejected("malformed delivery id")
        if not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12:
            raise WebhookRejected("malformed timestamp")
        if abs(time.time() - int(timestamp)) > WEBHOOK_TOLERANCE:
            raise WebhookRejected("timestamp outside the tolerance")
        companies = self.env["res.company"].sudo().search([("openmes_webhook_secret", "!=", False)], order="id")
        if not companies:
            raise WebhookRejected("no webhook registered")
        signed = signature.encode("utf-8", "replace")
        company = None
        for candidate in companies:
            expected = "sha256=" + hmac.new(
                candidate.openmes_webhook_secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
            ).hexdigest()
            # Every secret is checked, so the time does not tell which matched.
            if hmac.compare_digest(expected.encode(), signed) and company is None:
                company = candidate
        if company is None:
            raise WebhookRejected("bad signature")
        return event, delivery, company

    @api.model
    def _openmes_seen(self, delivery, company):
        """Whether ``company`` received this delivery id already. Asked only
        after the signature: without the secret nobody can probe which ids exist."""
        return bool(self.env["openmes.webhook.event"].sudo().search_count(
            [("delivery_id", "=", delivery), ("company_id", "=", company.id)], limit=1
        ))

    # ── Processing ───────────────────────────────────────────────────────────

    @api.model
    def _openmes_receive(self, headers, raw_body):
        """Verify and queue one delivery. Returns ``(http status, json body)``;
        raises WebhookRejected for a 401.

        Nothing is applied here: the event is stored and the processing cron
        triggered, so the answer never waits for a large MO to close."""
        event_header, delivery, company = self._openmes_verify(headers, raw_body)
        if self._openmes_seen(delivery, company):
            # Authentic, received before: OpenMES retries when it did not get
            # our answer (a timeout). Acknowledge it, change nothing (API doc,
            # "Verifying", step 6); a non-2xx would show it as failed.
            return 200, {"status": "duplicate"}
        try:
            body = json.loads(raw_body)
        except ValueError:
            return 400, {"status": "invalid"}
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            return 400, {"status": "invalid"}
        event = body.get("event")
        # The body is signed, the header is not: they must agree.
        if event != event_header:
            raise WebhookRejected("event header does not match the body")

        if not company.openmes_enabled or event not in HANDLED_EVENTS:
            # webhook.test and events this module does not subscribe to.
            return 200, {"status": "ignored"}
        data = body["data"]
        order_no = data.get("order_no") or data.get("work_order_no")
        try:
            with self.env.cr.savepoint():
                # Two copies at the same moment: the second waits here for the
                # first one and then fails the unique constraint.
                self.env["openmes.webhook.event"].sudo().create({
                    "company_id": company.id,
                    "event": event,
                    "delivery_id": delivery,
                    "order_no": str(order_no)[:255] if order_no not in (None, "") else False,
                    "payload": data,
                })
                self.env["openmes.webhook.event"].flush_model()
        except psycopg2.errors.UniqueViolation:
            return 200, {"status": "duplicate"}
        self.env["openmes.webhook.event"]._openmes_cron()._trigger()
        return 200, {"status": "queued"}

    @api.model
    def _openmes_dispatch(self, event, data, company=None):
        """Apply one event of ``company`` (default: the current one). Returns
        False when there was nothing to do."""
        company = company or self.env.company
        if event in WORK_ORDER_EVENTS:
            work_order = self._openmes_find_work_order(data.get("order_no"), company)
            if not work_order:
                return False
            if not work_order._openmes_try_lock():
                # A send is writing its result on this order right now.
                raise WorkOrderBusy()
            # The same code as the status pull.
            work_order._openmes_apply_status(data)
            return True
        if event == "stock_document.posted":
            # Nothing from the payload is booked: the cron fetches the documents
            # from OpenMES itself, books them and acknowledges them.
            self.env.ref("openmes_connector.ir_cron_openmes_stock_documents").sudo()._trigger()
            return True
        if event == "quality_issue.created":
            return self._openmes_note_quality_issue(data, company)
        # webhook.test and events this module does not subscribe to.
        return False

    @api.model
    def _openmes_find_work_order(self, order_no, company):
        """Only orders of ``company``: another company's instance cannot touch them."""
        if not isinstance(order_no, str) or not order_no or len(order_no) > 255:
            return self.env["openmes.work.order"]
        return self.env["openmes.work.order"].sudo().search(
            [("name", "=", order_no), ("company_id", "=", company.id)], limit=1
        )

    @api.model
    def _openmes_note_quality_issue(self, data, company):
        """Post a plain-text note (escaped by message_post) on the work order."""
        work_order = self._openmes_find_work_order(data.get("work_order_no"), company)
        if not work_order:
            return False
        qty = data.get("non_conforming_qty")
        qty = float_round(float(qty), precision_digits=4) if qty is not None else None

        def text(key, limit=200):
            value = data.get(key)
            return str(value)[:limit] if value not in (None, "") else "-"

        message = _(
            "OpenMES reported a quality issue on work order %(order)s: %(title)s "
            "(type: %(type)s, severity: %(severity)s, non-conforming quantity: %(qty)s).",
            order=work_order.name,
            title=text("title", 255),
            type=text("issue_type"),
            severity=text("severity", 50),
            qty=qty if qty is not None else "-",
        )
        work_order.message_post(body=message)
        return True
