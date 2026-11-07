import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import RedirectWarning, UserError, ValidationError
from odoo.tools import float_compare, float_round, split_every

from .openmes_client import CONCURRENCY_ERRORS, OpenmesApiError, parse_iso_datetime

_logger = logging.getLogger(__name__)

# Mirrors App\Models\WorkOrder::STATUSES in OpenMES.
MES_STATUSES = [
    ("PENDING", "Pending"),
    ("ACCEPTED", "Accepted"),
    ("IN_PROGRESS", "In progress"),
    ("BLOCKED", "Blocked"),
    ("PAUSED", "Paused"),
    ("CHANGE_HOLD", "Change hold"),
    ("DONE", "Done"),
    ("REJECTED", "Rejected"),
    ("CANCELLED", "Cancelled"),
]
MES_TERMINAL_STATUSES = ("DONE", "REJECTED", "CANCELLED")

# Mirrors App\Enums\ChangeRequestType / ChangeRequestStatus in OpenMES.
CHANGE_REQUEST_TYPES = [("CHANGE", "Change"), ("CANCEL", "Cancellation")]
CHANGE_REQUEST_STATUSES = [
    ("DRAFT", "Draft"),
    ("SUBMITTED", "Waiting for approval"),
    ("APPROVED", "Approved"),
    ("REJECTED", "Rejected"),
    ("APPLIED", "Applied"),
    ("CANCELLED", "Withdrawn"),
]
CHANGE_REQUEST_OPEN = ("DRAFT", "SUBMITTED", "APPROVED")

# ImportWorkOrdersRequest::MAX_ORDERS
MAX_ORDERS_PER_REQUEST = 1000
# Give up retrying automatically after this many failed sends; "Send" still works.
MAX_AUTO_ATTEMPTS = 5
# Re-read a little before the last seen change, in case one landed while we
# were paging.
POLL_OVERLAP = timedelta(minutes=2)
# A send that met a work order being changed by someone else (a webhook event)
# is tried again this much later.
BUSY_RETRY_DELAY = timedelta(seconds=60)
EXTERNAL_SYSTEM = "odoo"


class OpenmesWorkOrder(models.Model):
    _name = "openmes.work.order"
    _description = "OpenMES Work Order"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(
        "OpenMES Order No.",
        required=True,
        readonly=True,
        index=True,
        copy=False,
        help="order_no of the work order in OpenMES. Unique and stable, so a "
        "repeated send never creates a duplicate.",
    )
    source_type = fields.Selection(
        [("sale_line", "Sale Order Line"), ("production", "Manufacturing Order")],
        "Source",
        required=True,
        default="sale_line",
        readonly=True,
        index=True,
        help="What the work order was created from: a confirmed sale order line or "
        "a confirmed manufacturing order.",
    )
    sale_order_id = fields.Many2one("sale.order", readonly=True, index=True, ondelete="cascade")
    sale_line_id = fields.Many2one("sale.order.line", readonly=True, ondelete="set null")
    production_id = fields.Many2one(
        "mrp.production", "Manufacturing Order", readonly=True, index=True, ondelete="cascade", copy=False
    )
    production_state = fields.Selection(
        related="production_id.state",
        string="Manufacturing Order Status",
        help="Status of the manufacturing order in Odoo.",
    )
    origin_sale_order_id = fields.Many2one(
        "sale.order",
        "Customer Sale Order",
        readonly=True,
        index=True,
        ondelete="set null",
        help="The sale order the manufacturing order was made for, if any.",
    )
    source_name = fields.Char("Source Document", compute="_compute_source_name", compute_sudo=True)
    company_id = fields.Many2one(
        "res.company", compute="_compute_company_id", store=True, index=True, compute_sudo=True
    )
    partner_id = fields.Many2one(
        "res.partner", "Customer", compute="_compute_partner_id", store=True, index=True, compute_sudo=True
    )
    product_id = fields.Many2one("product.product", required=True, readonly=True)
    product_uom_id = fields.Many2one(related="product_id.uom_id", string="Unit of Measure")
    line_code = fields.Char(
        "OpenMES Line Code",
        tracking=True,
        help="Code of the OpenMES production line the order is planned on.",
    )
    planned_qty = fields.Float(
        "Planned Quantity",
        digits="Product Unit of Measure",
        readonly=True,
        tracking=True,
        help="The quantity Odoo asks for. While a change request is open it can "
        "differ from the quantity OpenMES is producing.",
    )
    mes_planned_qty = fields.Float(
        "Planned in OpenMES",
        digits="Product Unit of Measure",
        readonly=True,
        help="The quantity OpenMES is currently planned to produce.",
    )
    produced_qty = fields.Float("Produced Quantity", digits="Product Unit of Measure", readonly=True)
    state = fields.Selection(
        [
            ("to_send", "To Send"),
            ("sent", "Sent"),
            ("error", "Error"),
            ("cancelled", "Cancelled"),
        ],
        default="to_send",
        required=True,
        readonly=True,
        index=True,
        tracking=True,
    )
    mes_status = fields.Selection(MES_STATUSES, "OpenMES Status", readonly=True, tracking=True)
    mes_id = fields.Integer("OpenMES ID", readonly=True, copy=False)
    pending_cancel = fields.Boolean(
        "Cancellation to Send",
        readonly=True,
        copy=False,
        help="The source document was cancelled; the cancellation is waiting to be sent to OpenMES.",
    )
    change_request_id = fields.Integer("OpenMES Change Request", readonly=True, copy=False)
    change_request_type = fields.Selection(CHANGE_REQUEST_TYPES, readonly=True, copy=False)
    change_request_status = fields.Selection(
        CHANGE_REQUEST_STATUSES, "Change Request Status", readonly=True, copy=False, tracking=True
    )
    sent_date = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)
    mes_updated_at = fields.Datetime(
        "Last Change in OpenMES",
        readonly=True,
        copy=False,
        help="updated_at of the newest OpenMES state applied. An older state (a late "
        "webhook or a re-read) never overwrites a newer one.",
    )
    attempt_count = fields.Integer(readonly=True)
    last_error = fields.Text(readonly=True)

    _sql_constraints = [
        ("name_unique", "unique(name)", "An OpenMES order number must be unique."),
    ]

    @api.depends("sale_order_id.company_id", "production_id.company_id")
    def _compute_company_id(self):
        for record in self:
            record.company_id = record.sale_order_id.company_id or record.production_id.company_id

    @api.depends("sale_order_id.partner_id", "origin_sale_order_id.partner_id")
    def _compute_partner_id(self):
        for record in self:
            record.partner_id = (record.sale_order_id or record.origin_sale_order_id).partner_id

    @api.depends("sale_order_id.name", "production_id.name")
    def _compute_source_name(self):
        for record in self:
            record.source_name = (record.sale_order_id or record.production_id).name or False

    @api.constrains("source_type", "sale_order_id", "production_id")
    def _check_source(self):
        for record in self:
            if record.source_type == "production":
                valid = record.production_id and not record.sale_order_id
            else:
                valid = record.sale_order_id and not record.production_id
            if not valid:
                raise ValidationError(
                    _("An OpenMES work order needs exactly one source: a sale order or a manufacturing order.")
                )

    @api.constrains("line_code")
    def _check_line_code(self):
        for record in self:
            code = record.line_code
            if code and (len(code) > 100 or code != code.strip()):
                raise ValidationError(
                    _("The OpenMES line code must be at most 100 characters, without leading or trailing spaces.")
                )

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_send(self):
        self.check_access("write")
        self.write({"attempt_count": 0})
        busy = self._openmes_send()
        busy |= self._openmes_send_cancellations()
        failed = self.filtered(lambda r: r.state == "error" or (r.pending_cancel and r.last_error)) - busy
        if busy:
            return self._notify(
                _(
                    "%(count)s work order(s) are being updated right now; they are sent again within a minute.",
                    count=len(busy),
                ),
                "warning",
            )
        if failed:
            return self._notify(
                _("%(count)s work order(s) could not be sent. See the error on each order.", count=len(failed)),
                "warning",
            )
        return self._notify(_("Work orders sent to OpenMES."), "success")

    def action_cancel(self):
        self.check_access("write")
        self.filtered(lambda r: r.state in ("to_send", "error") and not r.mes_id).write({"state": "cancelled"})

    def action_reset_to_send(self):
        self.check_access("write")
        self.filtered(lambda r: r.state in ("error", "cancelled") and not r.pending_cancel).write(
            {"state": "to_send", "attempt_count": 0, "last_error": False}
        )

    def _notify(self, message, kind):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "message": message,
                "type": kind,
                "sticky": kind != "success",
                # Reload the open form/list so the new state shows at once.
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _openmes_by_company(self):
        """``[(company, records)]``: each company talks to its own OpenMES."""
        groups = {}
        for record in self:
            groups.setdefault(record.company_id or self.env.company, []).append(record.id)
        return [(company, self.browse(ids)) for company, ids in groups.items()]

    def _source_document(self):
        """The record the work order comes from: its sale order or manufacturing order."""
        self.ensure_one()
        return self.sale_order_id or self.production_id

    def _post_on_source(self, body):
        # Users who send work orders do not necessarily have access to the
        # source document (e.g. a sales manager and an MO); the author stays theirs.
        for record in self:
            record._source_document().sudo().message_post(body=body)

    # ── Sending ──────────────────────────────────────────────────────────────

    def _openmes_check_ready(self):
        """Return why this order cannot be sent, or None."""
        self.ensure_one()
        if not self.product_id.default_code:
            return _(
                "Product %(product)s has no Internal Reference. It is sent to OpenMES "
                "as the product type code.",
                product=self.product_id.display_name,
            )
        if not self.line_code:
            return _("No OpenMES line code: set one on the product or a default line in Settings.")
        if float_compare(self.planned_qty, 0.01, precision_digits=2) < 0:
            return _("Planned quantity must be at least 0.01.")
        return None

    def _openmes_prepare_payload(self):
        self.ensure_one()
        if self.source_type == "production":
            return self._openmes_prepare_production_payload()
        order = self.sale_order_id
        line = self.sale_line_id
        payload = {
            "order_no": self.name,
            "line_code": self.line_code,
            "product_type_code": self.product_id.default_code,
            "planned_qty": float_round(self.planned_qty, precision_digits=4),
            "customer_order_no": (order.client_order_ref or order.name)[:100],
            "description": (line.name or self.product_id.display_name or "")[:2000] or None,
            "external_system": EXTERNAL_SYSTEM,
            "external_ref": f"sale.order.line,{line.id}" if line else f"openmes.work.order,{self.id}",
            "customer_name": (order.partner_id.commercial_partner_id.name or "")[:255] or None,
            "extra_data": {"sale_order": order.name},
            "change_reason": _("Changed in Odoo sale order %(order)s.", order=order.name),
        }
        if line and line.product_uom_qty:
            # Net price per product UoM. OpenMES stores two decimals and treats
            # any other price on an order in production as a locked-field change,
            # so round the same way it does.
            qty = line.product_uom._compute_quantity(line.product_uom_qty, self.product_id.uom_id, round=False)
            if qty:
                payload["unit_price"] = max(0.0, float_round(line.price_subtotal / qty, precision_digits=2))
                payload["price_currency"] = order.currency_id.name
        due = order.commitment_date or order.expected_date
        if due:
            payload["due_date"] = fields.Date.to_string(due.date())
        return payload

    def _openmes_prepare_production_payload(self):
        """Payload of a work order made from a manufacturing order.

        No unit price: an MO has none, and a missing key means "no opinion" to
        OpenMES, while a different price on an order in production is an error.
        """
        production = self.production_id
        order = self.origin_sale_order_id
        line = production._openmes_origin_sale_line()
        extra = {"manufacturing_order": production.name}
        if order:
            extra["sale_order"] = order.name
        payload = {
            "order_no": self.name,
            "line_code": self.line_code,
            "product_type_code": self.product_id.default_code,
            "planned_qty": float_round(self.planned_qty, precision_digits=4),
            "description": (line.name or self.product_id.display_name or "")[:2000] or None,
            "external_system": EXTERNAL_SYSTEM,
            "external_ref": f"mrp.production,{production.id}",
            "extra_data": extra,
            "change_reason": _("Changed in Odoo manufacturing order %(order)s.", order=production.name),
        }
        if order:
            payload["customer_order_no"] = (order.client_order_ref or order.name)[:100]
            payload["customer_name"] = (order.partner_id.commercial_partner_id.name or "")[:255] or None
        due = production._openmes_due_date()
        if due:
            payload["due_date"] = fields.Date.to_string(due)
        return payload

    def _openmes_mark_error(self, message):
        for record in self:
            record.write(
                {
                    "state": "error",
                    "last_error": message,
                    "attempt_count": record.attempt_count + 1,
                }
            )

    def _openmes_apply_result(self, result, now):
        """Store one row of the import report on this order."""
        self.ensure_one()
        vals = {
            "state": "sent",
            "sent_date": now,
            "last_error": False,
            "attempt_count": 0,
            "mes_status": self.mes_status or "PENDING",
        }
        if result.get("id"):
            vals["mes_id"] = int(result["id"])
        action = result.get("action")
        if action in ("created", "updated"):
            vals["mes_planned_qty"] = self.planned_qty
        if action == "change_requested" and result.get("change_request_id"):
            is_new = int(result["change_request_id"]) != self.change_request_id
            vals.update(
                {
                    "change_request_id": int(result["change_request_id"]),
                    "change_request_type": "CHANGE",
                    "change_request_status": "SUBMITTED",
                }
            )
            self.write(vals)
            if is_new:
                self._post_on_source(
                    _(
                        "OpenMES work order %(order)s is already in production, so the change "
                        "(planned %(qty)s %(uom)s) was sent as a change request. It applies once "
                        "a manager approves it in OpenMES.",
                        order=self.name,
                        qty=self.planned_qty,
                        uom=self.product_uom_id.name,
                    )
                )
            return
        self.write(vals)

    def _openmes_try_lock(self):
        """Lock the rows of these work orders for this transaction, without
        waiting. Returns the ones locked; the others are being changed by
        another transaction right now (a webhook event, another send) or were
        changed since this transaction started.

        Taken before a send: the import report is then written on rows nobody
        else can change in the meantime, so the result of a successful send is
        never lost to a serialization failure after the HTTP call.
        """
        if not self:
            return self

        def lock(records):
            try:
                with self.env.cr.savepoint(flush=False):
                    # Row locking has no ORM equivalent; only ids are passed.
                    self.env.cr.execute(
                        "SELECT id FROM openmes_work_order WHERE id IN %s FOR NO KEY UPDATE NOWAIT",
                        (tuple(records.ids),),
                        log_exceptions=False,
                    )
            except CONCURRENCY_ERRORS:
                return False
            return True

        # Usually nobody else holds them: one statement. Otherwise row by row.
        if lock(self):
            return self
        if len(self) == 1:
            return self.browse()
        return self.browse([record.id for record in self if lock(record)])

    def _openmes_retry_busy(self, busy):
        """Send ``busy`` again a little later: they were being changed by another
        transaction. Their state and attempts are left as they were."""
        if not busy:
            return
        _logger.info("OpenMES send: work orders %s are being changed elsewhere; retried shortly", busy.ids)
        self.env.ref("openmes_connector.ir_cron_openmes_send").sudo()._trigger(
            fields.Datetime.now() + BUSY_RETRY_DELAY
        )

    def _openmes_send(self):
        """Push the orders that are waiting (or failed) to OpenMES.

        Uses ``update_or_create``: the order number is deterministic, so a
        retry after a lost response never duplicates an order. OpenMES applies
        a change at once while the order has not started; once it is in
        production the change becomes a change request a manager approves.

        Returns the orders skipped because another transaction was changing
        them; they are sent again by the cron within a minute.
        """
        pending = self.filtered(lambda r: r.state in ("to_send", "error") and not r.pending_cancel)
        locked = pending._openmes_try_lock()
        busy = pending - locked
        pending = locked
        self._openmes_retry_busy(busy)
        # Finished in OpenMES while an update was queued: nothing left to change there.
        finished = pending.filtered(lambda r: r.mes_status in MES_TERMINAL_STATUSES)
        finished.write({"state": "sent", "attempt_count": 0, "last_error": False})
        pending -= finished
        ready = self.browse()
        for record in pending:
            problem = record._openmes_check_ready()
            if problem:
                record._openmes_mark_error(problem)
            else:
                ready |= record

        for company, records in ready._openmes_by_company():
            client = self.env["openmes.client"].sudo().with_company(company)
            for chunk in split_every(MAX_ORDERS_PER_REQUEST, records.ids, self.browse):
                busy |= records._openmes_send_chunk(client, chunk)
        return busy

    def _openmes_send_chunk(self, client, chunk):
        body = {
            "strategy": "update_or_create",
            "orders": [record._openmes_prepare_payload() for record in chunk],
        }
        try:
            report = client._post_import("/work-orders/import", body)
        except OpenmesApiError as exc:
            chunk._openmes_mark_error(str(exc))
            return self.browse()

        errors_by_row = {}
        for error in report["errors"]:
            field = error.get("field")
            message = error.get("message") or _("Unknown error")
            errors_by_row.setdefault(error.get("row"), []).append(f"{field}: {message}" if field else message)
        results_by_row = {res.get("row"): res for res in report["results"]}

        now = fields.Datetime.now()
        busy = self.browse()
        for row, record in enumerate(chunk, start=1):
            try:
                with self.env.cr.savepoint():
                    if row in errors_by_row:
                        record._openmes_mark_error("; ".join(errors_by_row[row]))
                    else:
                        record._openmes_apply_result(results_by_row.get(row, {}), now)
                    record.flush_recordset()
            except CONCURRENCY_ERRORS:
                # The rows are locked before the call, so this should not
                # happen; if it does, the order keeps its state and attempts and
                # is sent again (update_or_create makes that harmless).
                self.env.invalidate_all()
                _logger.warning("OpenMES send: the result of work order %s could not be saved; resent", record.id)
                busy |= record
        self._openmes_retry_busy(busy)
        return busy

    def _openmes_send_cancellations(self):
        """Send the cancellations of cancelled sale orders to OpenMES.
        Returns the ones skipped because another transaction was changing them."""
        pending = self.filtered("pending_cancel")
        locked = pending._openmes_try_lock()
        busy = pending - locked
        self._openmes_retry_busy(busy)
        for record in locked:
            client = self.env["openmes.client"].sudo().with_company(record.company_id or self.env.company)
            if record.source_type == "production":
                reason = _("Manufacturing order %(order)s was cancelled in Odoo.", order=record.production_id.name)
            else:
                reason = _("Sale order %(order)s was cancelled in Odoo.", order=record.sale_order_id.name)
            try:
                data = client._cancel_work_order(record.name, reason)
            except OpenmesApiError as exc:
                if exc.status_code == 409:
                    # Already finished in OpenMES: nothing left to cancel.
                    record.write({"pending_cancel": False, "last_error": str(exc)})
                    record._post_on_source(
                        _("OpenMES did not cancel work order %(order)s: %(reason)s", order=record.name, reason=str(exc))
                    )
                elif exc.status_code == 404:
                    # Never reached OpenMES (or was removed there).
                    record.write({"pending_cancel": False, "state": "cancelled", "last_error": False})
                else:
                    record.write({"last_error": str(exc), "attempt_count": record.attempt_count + 1})
                continue

            work_order = data.get("work_order") or {}
            vals = {"pending_cancel": False, "last_error": False, "attempt_count": 0}
            if work_order.get("id"):
                vals["mes_id"] = int(work_order["id"])
            if data.get("result") == "cancelled":
                vals.update({"state": "cancelled", "mes_status": "CANCELLED"})
                record.write(vals)
                record._post_on_source(_("OpenMES work order %(order)s was cancelled.", order=record.name))
            elif data.get("result") == "change_requested":
                vals.update(
                    {
                        "change_request_id": int(data.get("change_request_id") or 0),
                        "change_request_type": "CANCEL",
                        "change_request_status": (data.get("change_request") or {}).get("status") or "SUBMITTED",
                    }
                )
                record.write(vals)
                record._post_on_source(
                    _(
                        "OpenMES work order %(order)s is in production, so its cancellation was "
                        "sent as a change request. It is cancelled once a manager approves it.",
                        order=record.name,
                    )
                )
            else:
                record.write({"last_error": _("Unexpected answer from OpenMES to the cancellation.")})
        return busy

    @api.model
    def _cron_openmes_send(self):
        # sudo: the cron user is not necessarily a member of every company.
        self.env["res.company"]._openmes_cron_each(
            "work order send", lambda company: self.sudo().with_company(company)._openmes_cron_send(company)
        )

    @api.model
    def _openmes_cron_send(self, company):
        records = self.search(
            [
                ("company_id", "=", company.id),
                "|",
                "|",
                ("state", "=", "to_send"),
                "&",
                ("state", "=", "error"),
                ("attempt_count", "<", MAX_AUTO_ATTEMPTS),
                "&",
                ("pending_cancel", "=", True),
                ("attempt_count", "<", MAX_AUTO_ATTEMPTS),
            ]
        )
        records._openmes_send()
        records._openmes_send_cancellations()

    # ── Changes from the source document ─────────────────────────────────────

    def _openmes_queue_cancellation(self):
        """The source document was cancelled: cancel its work orders too."""
        work_orders = self.filtered(lambda w: w.state != "cancelled")
        # Never reached OpenMES: drop locally.
        unsent = work_orders.filtered(lambda w: not w.mes_id and w.state in ("to_send", "error"))
        unsent.write({"state": "cancelled", "last_error": False})
        # Known to OpenMES and not finished: cancel there too. OpenMES cancels
        # at once before production starts, otherwise it asks a manager.
        to_cancel = (work_orders - unsent).filtered(lambda w: w.mes_status not in MES_TERMINAL_STATUSES)
        if not to_cancel:
            return
        to_cancel.write({"pending_cancel": True, "attempt_count": 0, "last_error": False})
        by_source = {}
        for work_order in to_cancel:
            by_source.setdefault(work_order._source_document(), []).append(work_order.name)
        for source, names in by_source.items():
            source.sudo().message_post(
                body=_(
                    "The cancellation of OpenMES work orders %(orders)s is being sent to OpenMES.",
                    orders=", ".join(names),
                )
            )
        self.env.ref("openmes_connector.ir_cron_openmes_send").sudo()._trigger()

    def _openmes_request_update(self, vals):
        """Queue a resend after the source document changed (quantity, dates)."""
        updatable = self.filtered(
            lambda r: r.state != "cancelled" and not r.pending_cancel and r.mes_status not in MES_TERMINAL_STATUSES
        )
        if not updatable:
            return
        updatable.write(dict(vals, state="to_send", attempt_count=0))
        self.env.ref("openmes_connector.ir_cron_openmes_send").sudo()._trigger()

    # ── Pulling state back ───────────────────────────────────────────────────

    def _openmes_apply_status(self, row):
        """Apply one OpenMES work order state (status pull row or webhook payload)."""
        self.ensure_one()
        vals = {}
        updated = parse_iso_datetime(row.get("updated_at"))
        if updated and self.mes_updated_at:
            if updated < self.mes_updated_at:
                # Out of order (a retried webhook, a re-read): a newer state is applied already.
                return
        if updated and updated != self.mes_updated_at:
            vals["mes_updated_at"] = updated
        status = row.get("status")
        if status in dict(MES_STATUSES) and status != self.mes_status:
            vals["mes_status"] = status
        if row.get("id") and int(row["id"]) != self.mes_id:
            vals["mes_id"] = int(row["id"])
        for key in ("produced_qty", "planned_qty"):
            value = row.get(key)
            if value is None:
                continue
            target = "produced_qty" if key == "produced_qty" else "mes_planned_qty"
            if float_compare(float(value), self[target], precision_digits=4):
                vals[target] = float(value)
        completed = parse_iso_datetime(row.get("completed_at"))
        if completed != self.completed_at:
            vals["completed_at"] = completed
        if status == "CANCELLED" and self.state != "cancelled":
            vals["state"] = "cancelled"
        if not vals:
            return
        self.write(vals)
        if "mes_status" in vals:
            self._post_on_source(
                _(
                    "OpenMES work order %(order)s is now %(status)s (produced %(produced)s of %(planned)s %(uom)s).",
                    order=self.name,
                    status=dict(MES_STATUSES)[self.mes_status],
                    produced=self.produced_qty,
                    planned=self.mes_planned_qty or self.planned_qty,
                    uom=self.product_uom_id.name,
                )
            )
        if vals.get("mes_status") == "DONE" and self.production_id:
            # By source, not by the current mode: MO work orders created before
            # a switch back to sale lines still close their MO.
            self._openmes_finish_production()

    # ── Closing the manufacturing order ──────────────────────────────────────

    def action_openmes_finish_production(self):
        """Retry closing the MO after OpenMES finished (e.g. once lots are set)."""
        self.check_access("write")
        records = self.filtered(lambda r: r.production_id and r.mes_status == "DONE")
        records.production_id.check_access("write")
        records._openmes_finish_production()
        failed = records.filtered(lambda r: r.production_id.state != "done")
        if failed:
            return self._notify(
                _("%(count)s manufacturing order(s) could not be marked as done. See the error on each order.",
                  count=len(failed)),
                "warning",
            )
        return self._notify(_("Manufacturing orders marked as done."), "success")

    def _openmes_finish_production(self):
        """Mark the MO done with the quantity OpenMES produced.

        Runs from the status cron, so it never raises: a failure (missing lot,
        a confirmation Odoo wants from a person…) is written on the work order
        and the MO, and the MO stays open for a manual close.
        """
        for record in self.filtered("production_id"):
            # Pickings, moves and the backorder belong to the MO's company,
            # not to the cron user's default one.
            production = record.production_id.with_company(record.production_id.company_id)
            if production.state in ("done", "cancel"):
                continue
            if float_compare(record.produced_qty, 0.0, precision_digits=4) <= 0:
                message = _(
                    "OpenMES finished work order %(order)s with nothing produced; the manufacturing "
                    "order was left open.",
                    order=record.name,
                )
                record.last_error = message
                record._post_on_source(message)
                continue
            uom = production.product_uom_id
            qty = record.product_id.uom_id._compute_quantity(record.produced_qty, uom, rounding_method="HALF-UP")
            under_produced = float_compare(qty, production.product_qty, precision_rounding=uom.rounding) < 0
            siblings = production.procurement_group_id.mrp_production_ids
            error = None
            try:
                with self.env.cr.savepoint():
                    production.qty_producing = qty
                    production._set_qty_producing()
                with self.env.cr.savepoint():
                    production.with_context(
                        skip_backorder=True,
                        mo_ids_to_backorder=production.ids if under_produced else [],
                    ).button_mark_done()
                    if production.state != "done":
                        # A wizard (e.g. the consumption warning) wants a person.
                        raise UserError(_("Odoo needs a manual confirmation to close it."))
            except CONCURRENCY_ERRORS:
                # Another transaction changed the same rows: not a reason to
                # leave the MO open, the caller runs it again.
                raise
            except (UserError, RedirectWarning) as exc:
                error = str(exc)
            except Exception:
                # Anything else (a third-party override…) must not stop the
                # status pull for every other order; details go to the log only.
                _logger.exception("Closing manufacturing order %s for OpenMES failed", production.id)
                error = _("Unexpected error; see the server log.")
            if error:
                self.env.invalidate_all()
                record.last_error = _(
                    "Odoo could not mark the manufacturing order as done: %(error)s", error=error
                )
                record._post_on_source(
                    _(
                        "OpenMES finished work order %(order)s (%(qty)s %(uom)s produced), but Odoo could "
                        "not mark the manufacturing order as done: %(error)s",
                        order=record.name,
                        qty=record.produced_qty,
                        uom=record.product_uom_id.name,
                        error=error,
                    )
                )
                continue
            record.last_error = False
            backorder = production.procurement_group_id.mrp_production_ids - siblings
            if under_produced and backorder:
                # The backorder (with its own work order) carries the rest: this
                # work order planned what was made, so it is no quantity difference.
                record.planned_qty = record.produced_qty
                message = _(
                    "Marked as done with %(qty)s %(uom)s produced in OpenMES (work order %(order)s); "
                    "the rest is in backorder %(backorder)s.",
                    qty=qty,
                    uom=uom.name,
                    order=record.name,
                    backorder=", ".join(backorder.mapped("name")),
                )
            else:
                message = _(
                    "Marked as done with %(qty)s %(uom)s produced in OpenMES (work order %(order)s).",
                    qty=qty,
                    uom=uom.name,
                    order=record.name,
                )
            record._post_on_source(message)
            # Its stock documents waited for the close: book them now, so they
            # stop holding back the stock snapshot.
            self.env.ref("openmes_connector.ir_cron_openmes_stock_documents").sudo()._trigger()

    def _openmes_apply_change_request(self, row):
        self.ensure_one()
        status = row.get("status")
        if status not in dict(CHANGE_REQUEST_STATUSES) or status == self.change_request_status:
            return
        self.write({"change_request_status": status})
        cancellation = self.change_request_type == "CANCEL"
        if status == "APPLIED":
            message = (
                _("OpenMES applied the cancellation of work order %(order)s.", order=self.name)
                if cancellation
                else _("OpenMES applied the change of work order %(order)s.", order=self.name)
            )
        elif status == "APPROVED":
            message = (
                _("OpenMES approved the cancellation of work order %(order)s.", order=self.name)
                if cancellation
                else _("OpenMES approved the change of work order %(order)s.", order=self.name)
            )
        elif status == "REJECTED":
            reason = row.get("rejection_reason") or _("no reason given")
            message = (
                _("OpenMES rejected the cancellation of work order %(order)s: %(reason)s", order=self.name, reason=reason)
                if cancellation
                else _("OpenMES rejected the change of work order %(order)s: %(reason)s", order=self.name, reason=reason)
            )
        else:
            return
        self._post_on_source(message)

    @api.model
    def _openmes_since(self, company, field, records, date_field):
        stored = company.sudo()[field]
        if stored:
            return stored, stored - POLL_OVERLAP
        dates = [d for d in records.mapped(date_field) if d]
        return False, (min(dates) if dates else fields.Datetime.now()) - POLL_OVERLAP

    @api.model
    def _cron_openmes_pull_status(self):
        def pull(company):
            model = self.sudo().with_company(company)
            client = model.env["openmes.client"]
            model._openmes_pull_orders(client)
            model._openmes_pull_change_requests(client)

        self.env["res.company"]._openmes_cron_each("status pull", pull)

    @api.model
    def _openmes_pull_orders(self, client):
        # Everything OpenMES knows that is not finished — including orders with
        # an update queued (to_send) or a cancellation waiting for approval.
        # Only orders of the client's company: another company's instance
        # may use the same order numbers.
        company = client.env.company
        tracked = self.sudo().search(
            [
                ("company_id", "=", company.id),
                ("mes_status", "not in", MES_TERMINAL_STATUSES),
                "|",
                ("state", "=", "sent"),
                ("mes_id", "!=", 0),
            ]
        )
        if not tracked:
            return
        stored, since = self._openmes_since(company, "openmes_status_since", tracked, "sent_date")
        by_name = {record.name: record for record in tracked}
        latest = stored

        def rows():
            params = {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
            try:
                yield from client._iter_export("/production/completions", dict(params, status="all"))
            except OpenmesApiError as exc:
                if exc.status_code != 422:
                    raise
                # OpenMES before multi-status export: one request per status.
                for status, _label in MES_STATUSES:
                    yield from client._iter_export("/production/completions", dict(params, status=status))

        try:
            for row in rows():
                updated = parse_iso_datetime(row.get("updated_at"))
                if updated and (not latest or updated > latest):
                    latest = updated
                record = by_name.get(row.get("order_no"))
                if record:
                    record._openmes_apply_status(row)
        except OpenmesApiError as exc:
            _logger.warning("OpenMES status pull failed: %s", exc)
            return

        if latest and latest != stored:
            company.sudo().openmes_status_since = latest

    @api.model
    def _openmes_pull_change_requests(self, client):
        company = client.env.company
        tracked = self.sudo().search([
            ("company_id", "=", company.id),
            ("change_request_id", "!=", 0),
            ("change_request_status", "in", CHANGE_REQUEST_OPEN),
        ])
        if not tracked:
            return
        stored, since = self._openmes_since(company, "openmes_cr_since", tracked, "write_date")
        by_id = {record.change_request_id: record for record in tracked}
        latest = stored
        try:
            for row in client._iter_export("/change-requests", {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ")}):
                updated = parse_iso_datetime(row.get("updated_at"))
                if updated and (not latest or updated > latest):
                    latest = updated
                record = by_id.get(row.get("id"))
                if record:
                    record._openmes_apply_change_request(row)
        except OpenmesApiError as exc:
            # 404 = OpenMES without the change-request feed; nothing to follow.
            if exc.status_code != 404:
                _logger.warning("OpenMES change-request pull failed: %s", exc)
            return
        if latest and latest != stored:
            company.sudo().openmes_cr_since = latest
