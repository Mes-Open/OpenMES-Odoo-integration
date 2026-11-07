"""Quantity differences between Odoo and OpenMES, and the to-dos they raise.

OpenMES can end up planning or producing a different quantity than Odoo asks
for: a manager rejects a change request, or the line finishes with more or
less than planned. Nothing in Odoo is corrected automatically; the person
responsible for the sale order (or the manufacturing order) gets an activity,
and the work order is flagged until someone adjusts the order in Odoo or
accepts the OpenMES quantity.
"""

import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round

from .openmes_work_order import CHANGE_REQUEST_OPEN

_logger = logging.getLogger(__name__)

TODO_ACTIVITY = "mail.mail_activity_data_todo"


def _qty_str(qty, rounding):
    """A quantity rounded to its unit, without trailing zeros (10.002, 60)."""
    text = "%f" % float_round(qty or 0.0, precision_rounding=rounding or 0.01)
    return text.rstrip("0").rstrip(".") if "." in text else text


class OpenmesWorkOrder(models.Model):
    _inherit = "openmes.work.order"

    qty_discrepancy = fields.Selection(
        [
            ("plan", "OpenMES plans a different quantity"),
            ("production", "OpenMES produced a different quantity"),
        ],
        "Quantity Difference",
        compute="_compute_qty_discrepancy",
        store=True,
        index=True,
        help="Set when OpenMES plans a different quantity than Odoo once no change request is "
        "open any more, or when OpenMES finished with a different quantity than planned in Odoo.",
    )
    attention_change_request_id = fields.Integer(
        "Change Request with Activity",
        readonly=True,
        copy=False,
        help="The rejected OpenMES change request an activity was already created for.",
    )
    done_activity_scheduled = fields.Boolean(
        "Production Difference Activity Created",
        readonly=True,
        copy=False,
    )
    serial_activity_scheduled = fields.Boolean(
        "Manual Close Activity Created",
        readonly=True,
        copy=False,
    )

    @api.depends(
        "planned_qty",
        "mes_planned_qty",
        "produced_qty",
        "mes_status",
        "state",
        "pending_cancel",
        "change_request_status",
        "product_id.uom_id.rounding",
    )
    def _compute_qty_discrepancy(self):
        for record in self:
            rounding = record.product_id.uom_id.rounding or 0.01
            kind = False
            if (
                record.state != "cancelled"
                and not record.pending_cancel
                and record.mes_status not in ("REJECTED", "CANCELLED")
            ):
                if record.mes_status == "DONE":
                    if float_compare(record.produced_qty, record.planned_qty, precision_rounding=rounding):
                        kind = "production"
                elif (
                    record.state == "sent"
                    and record.change_request_status not in CHANGE_REQUEST_OPEN
                    # 0 = OpenMES never confirmed a plan (e.g. a skipped import).
                    and float_compare(record.mes_planned_qty, 0.0, precision_rounding=rounding) > 0
                    and float_compare(record.mes_planned_qty, record.planned_qty, precision_rounding=rounding)
                ):
                    kind = "plan"
            record.qty_discrepancy = kind

    # ── Accepting the OpenMES quantity ───────────────────────────────────────

    def action_openmes_accept_quantity(self):
        """Take the OpenMES quantity as the planned quantity in Odoo.

        Only the work order changes: nothing is sent to OpenMES, and the sale
        order or manufacturing order is left as it is for a person to adjust.
        """
        self.check_access("write")
        todo = self.filtered("qty_discrepancy")
        if not todo:
            raise UserError(_("These work orders have no quantity difference to accept."))
        for record in todo:
            old = record.planned_qty
            new = record.produced_qty if record.qty_discrepancy == "production" else record.mes_planned_qty
            record.write({"planned_qty": new})
            rounding = record.product_id.uom_id.rounding
            record._post_on_source(
                _(
                    "Planned quantity of OpenMES work order %(order)s set to the OpenMES quantity "
                    "%(new)s %(uom)s (was %(old)s %(uom)s). The sale order or manufacturing order "
                    "was not changed.",
                    order=record.name,
                    new=_qty_str(new, rounding),
                    old=_qty_str(old, rounding),
                    uom=record.product_uom_id.name,
                )
            )
        return self._notify(_("OpenMES quantity accepted on %(count)s work order(s).", count=len(todo)), "success")

    # ── Activities ───────────────────────────────────────────────────────────

    def _openmes_attention_user(self):
        """Who acts on a difference: the salesperson, or the MO's responsible."""
        self.ensure_one()
        if self.source_type == "production":
            candidates = [self.production_id.user_id, self.origin_sale_order_id.user_id]
        else:
            candidates = [self.sale_order_id.user_id]
        source = self._source_document()
        root = self.env.ref("base.user_root")
        for user in candidates:
            if not user or not user.active or user.share or user == root:
                continue
            readable = (
                source.with_user(user).with_context(allowed_company_ids=user.company_ids.ids).has_access("read")
            )
            if readable:
                return user
        return self.env["res.users"]

    def _openmes_schedule_attention(self, build):
        """Create a To-Do on the source document for the responsible user.

        ``build(record)`` returns ``(summary, text)``; it is called with the
        assignee's language. Runs from the cron, so it never raises.
        """
        self.ensure_one()
        user = self._openmes_attention_user()
        source = self._source_document()
        if not user:
            _logger.info(
                "No responsible user for %s,%s; no activity for OpenMES work order %s",
                source._name, source.id, self.id,
            )
            return False
        summary, text = build(self.with_context(lang=user.lang))
        try:
            with self.env.cr.savepoint():
                source.sudo().activity_schedule(
                    TODO_ACTIVITY,
                    user_id=user.id,
                    summary=summary,
                    # Markup escapes the text: the reason comes from OpenMES.
                    note=Markup("<p>%s</p>") % text,
                )
        except Exception:
            _logger.exception("Could not create an activity for OpenMES work order %s", self.id)
            return False
        return True

    def _openmes_rejection_texts(self, row):
        self.ensure_one()
        rounding = self.product_id.uom_id.rounding
        uom = self.product_uom_id.name
        reason = row.get("rejection_reason") or self.env._("no reason given")
        mes_qty = _qty_str(self.mes_planned_qty or self.planned_qty, rounding)
        if self.change_request_type == "CANCEL":
            summary = self.env._("OpenMES rejected a cancellation")
            text = self.env._(
                "OpenMES rejected the cancellation of work order %(order)s. Reason: %(reason)s. "
                "OpenMES keeps producing %(mes_qty)s %(uom)s although the order is cancelled in Odoo.",
                order=self.name,
                reason=reason,
                mes_qty=mes_qty,
                uom=uom,
            )
            return summary, text
        proposed = row.get("proposed") or {}
        requested = proposed.get("planned_qty") if isinstance(proposed, dict) else None
        try:
            requested = float(requested) if requested is not None else self.planned_qty
        except (TypeError, ValueError):
            requested = self.planned_qty
        summary = self.env._("OpenMES rejected a change")
        text = self.env._(
            "OpenMES rejected the change of work order %(order)s (requested %(requested)s %(uom)s). "
            "Reason: %(reason)s. OpenMES keeps planning %(mes_qty)s %(uom)s. "
            "Adjust the order in Odoo or ask again.",
            order=self.name,
            requested=_qty_str(requested, rounding),
            reason=reason,
            mes_qty=mes_qty,
            uom=uom,
        )
        return summary, text

    def _openmes_production_texts(self):
        self.ensure_one()
        rounding = self.product_id.uom_id.rounding
        diff = self.produced_qty - self.planned_qty
        over = diff > 0
        summary = (
            self.env._("OpenMES: over-production") if over else self.env._("OpenMES: under-production")
        )
        text = self.env._(
            "OpenMES finished work order %(order)s with %(produced)s %(uom)s instead of %(planned)s "
            "%(uom)s (%(diff)s %(uom)s). Check the delivery and the invoice.",
            order=self.name,
            produced=_qty_str(self.produced_qty, rounding),
            planned=_qty_str(self.planned_qty, rounding),
            diff=("+" if over else "-") + _qty_str(abs(diff), rounding),
            uom=self.product_uom_id.name,
        )
        return summary, text

    def _openmes_serial_close_texts(self):
        self.ensure_one()
        production = self.production_id
        summary = self.env._("OpenMES: close the manufacturing order manually")
        text = self.env._(
            "OpenMES finished work order %(order)s with %(qty)s %(uom)s produced. %(product)s is "
            "tracked by serial numbers, so Odoo did not close manufacturing order %(production)s: "
            "assign the serial numbers and mark it as done.",
            order=self.name,
            qty=_qty_str(self.produced_qty, self.product_id.uom_id.rounding),
            uom=self.product_uom_id.name,
            product=production.product_id.display_name,
            production=production.name,
        )
        return summary, text

    def _openmes_serial_left_open(self):
        """A serial-tracked MO is never closed from OpenMES: tell who closes it."""
        self.ensure_one()
        message = _(
            "The manufacturing order was not closed: serial-tracked products must be closed manually "
            "(OpenMES does not report serial numbers). OpenMES produced %(qty)s %(uom)s in work order "
            "%(order)s.",
            qty=_qty_str(self.produced_qty, self.product_id.uom_id.rounding),
            uom=self.product_uom_id.name,
            order=self.name,
        )
        self.last_error = message
        self._post_on_source(message)
        if not self.serial_activity_scheduled:
            # Marked before scheduling: an activity marked done is deleted, so
            # only this field keeps a retry from creating it again.
            self.serial_activity_scheduled = True
            self._openmes_schedule_attention(lambda record: record._openmes_serial_close_texts())

    # ── Hooks into the pull ──────────────────────────────────────────────────

    def _openmes_apply_change_request(self, row):
        before = self.change_request_status
        result = super()._openmes_apply_change_request(row)
        if (
            before != "REJECTED"
            and self.change_request_status == "REJECTED"
            and self.change_request_id
            and self.attention_change_request_id != self.change_request_id
        ):
            # Marked before scheduling: an activity marked done is deleted, so
            # only this field keeps the next pull from creating it again.
            self.attention_change_request_id = self.change_request_id
            self._openmes_schedule_attention(lambda record: record._openmes_rejection_texts(row))
        return result

    def _openmes_apply_status(self, row):
        before = self.mes_status
        # super() first: in MO mode it also closes the manufacturing order.
        result = super()._openmes_apply_status(row)
        if (
            before != "DONE"
            and self.mes_status == "DONE"
            and not self.done_activity_scheduled
            and float_compare(
                self.produced_qty, self.planned_qty, precision_rounding=self.product_id.uom_id.rounding or 0.01
            )
        ):
            self.done_activity_scheduled = True
            self._openmes_schedule_attention(lambda record: record._openmes_production_texts())
        return result
