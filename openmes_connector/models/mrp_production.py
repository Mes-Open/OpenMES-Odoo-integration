from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .openmes_client import DEFAULT_ORDER_PREFIX, SOURCE_MANUFACTURING
from .openmes_work_order import MES_TERMINAL_STATUSES

# MO fields whose change is sent to OpenMES (as planned_qty / due_date).
TRACKED_FIELDS = {"product_qty", "uom_id", "date_deadline", "date_start", "date_finished"}


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    openmes_work_order_ids = fields.One2many("openmes.work.order", "production_id", string="OpenMES Work Orders")
    openmes_work_order_count = fields.Integer(compute="_compute_openmes_work_order_count", compute_sudo=True)

    @api.depends("openmes_work_order_ids")
    def _compute_openmes_work_order_count(self):
        for production in self:
            production.openmes_work_order_count = len(production.openmes_work_order_ids)

    def action_view_openmes_work_orders(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("openmes_connector.action_openmes_work_order")
        action["domain"] = [("production_id", "=", self.id)]
        action["context"] = {"create": False}
        return action

    # ── Hooks ────────────────────────────────────────────────────────────────

    def action_confirm(self):
        # Also the entry point of MOs created by procurement (MTO, reordering rules).
        res = super().action_confirm()
        self._openmes_create_work_orders()
        return res

    def _split_productions(self, amounts=False, cancel_remaining_qty=False, set_consumed_qty=False):
        # Backorders (also the one after OpenMES produced less) and manual
        # splits are created without action_confirm.
        productions = super()._split_productions(
            amounts=amounts, cancel_remaining_qty=cancel_remaining_qty, set_consumed_qty=set_consumed_qty
        )
        # Result order: [orig_1, backorder_1a, …, orig_2, backorder_2a, …]. Only
        # the new MOs of an original that is itself in OpenMES get a work order:
        # an MO made before switching to this mode stays out of OpenMES, and so
        # do its backorders.
        new_productions = self.browse()
        in_openmes = False
        for production in productions:
            if production in self:
                in_openmes = bool(
                    production.sudo().openmes_work_order_ids.filtered(lambda w: w.state != "cancelled")
                )
            elif in_openmes:
                new_productions |= production
        new_productions._openmes_create_work_orders()
        return productions

    @api.ondelete(at_uninstall=False)
    def _unlink_except_openmes_work_order(self):
        # Deleting would cascade to the work orders and lose a cancellation
        # that has not reached OpenMES yet, while OpenMES keeps producing.
        live = self.sudo().openmes_work_order_ids.filtered(
            lambda w: w.pending_cancel
            or (w.state != "cancelled" and w.mes_id and w.mes_status not in MES_TERMINAL_STATUSES)
        )
        if live:
            raise UserError(
                _(
                    "Manufacturing orders with OpenMES work orders that are still open in OpenMES "
                    "cannot be deleted: %(orders)s. Wait until OpenMES confirms the cancellation.",
                    orders=", ".join(live.mapped("name")),
                )
            )

    def button_mark_done(self):
        res = super().button_mark_done()
        # Closed (by OpenMES or by hand, e.g. a serial-tracked product): a
        # "could not close" error of its finished work orders is obsolete.
        stale = self.filtered(lambda p: p.state == "done").sudo().openmes_work_order_ids.filtered(
            lambda w: w.mes_status == "DONE" and w.state == "sent" and w.last_error
        )
        if stale:
            stale.last_error = False
        return res

    def action_cancel(self):
        # Odoo 20 folded mrp.production._action_cancel into action_cancel; every
        # cancel path (button, stock move cancellation, unlink) goes through it.
        res = super().action_cancel()
        self.sudo().openmes_work_order_ids._openmes_queue_cancellation()
        return res

    def write(self, vals):
        before = self._openmes_snapshots() if TRACKED_FIELDS & set(vals) else {}
        res = super().write(vals)
        self._openmes_resend_changed(before)
        return res

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _openmes_planned_qty(self):
        self.ensure_one()
        return self.uom_id._compute_quantity(self.product_qty, self.product_id.uom_id, round=False)

    def _openmes_due_date(self):
        self.ensure_one()
        due = self.date_deadline or self.date_finished
        return due.date() if due else False

    def _openmes_snapshot(self):
        return self._openmes_planned_qty(), self._openmes_due_date()

    def _openmes_snapshots(self):
        """{id: (planned qty, due date)} of the MOs that have work orders."""
        return {p.id: p._openmes_snapshot() for p in self.sudo().filtered("openmes_work_order_ids")}

    def _openmes_resend_changed(self, before):
        """Queue a resend for the MOs whose quantity or due date changed since ``before``."""
        for production in self.sudo().browse(list(before)):
            if production.state in ("done", "cancel"):
                continue
            old_qty, old_due = before[production.id]
            new_qty, new_due = production._openmes_snapshot()
            if production.product_id.uom_id.compare(old_qty, new_qty) or old_due != new_due:
                production.openmes_work_order_ids._openmes_request_update({"planned_qty": new_qty})

    def _openmes_origin_sale_line(self):
        """The sale order line this MO was made for (MTO), if any."""
        self.ensure_one()
        production = self.sudo()
        line = production.move_dest_ids.sale_line_id[:1]
        if not line and "sale_line_id" in production._fields:
            line = production.sale_line_id
        return line

    def _openmes_backorder_family(self):
        """The MO and all its backorders (Odoo 19 links them by production group)."""
        self.ensure_one()
        return self.production_group_id.production_ids

    def _openmes_origin_sale_order(self):
        """The sale order this MO was made for, if any."""
        self.ensure_one()
        production = self.sudo()
        order = production._openmes_origin_sale_line().order_id
        if not order:
            # Odoo 19 stock references can link an MO to several sale orders;
            # only an unambiguous one counts (Odoo 18 had a single sale_id).
            sale_orders = production.reference_ids.sale_ids
            order = sale_orders if len(sale_orders) == 1 else order
        if not order and production.origin:
            # Last resort: "origin" is free text ("S00012", "S1, S2", an orderpoint…),
            # so only an exact match counts.
            order = self.env["sale.order"].sudo().search(
                [("name", "=", production.origin), ("company_id", "=", production.company_id.id)], limit=1
            )
        return order[:1]

    def _openmes_create_work_orders(self):
        vals_list = []
        names = set()
        for company in self.company_id:
            productions = self.filtered(lambda p: p.company_id == company)
            vals_list += productions._openmes_work_order_vals(company.sudo(), names)
        if vals_list:
            self.env["openmes.work.order"].sudo().create(vals_list)
            # Send after this transaction commits, not during the confirmation.
            self.env.ref("openmes_connector.ir_cron_openmes_send").sudo()._trigger()

    def _openmes_work_order_vals(self, company, names):
        if not company.openmes_enabled or company.openmes_order_source != SOURCE_MANUFACTURING:
            return []
        prefix = company.openmes_order_prefix or DEFAULT_ORDER_PREFIX
        # MRP users confirm MOs but only read OpenMES work orders.
        WorkOrder = self.env["openmes.work.order"].sudo()
        productions = self.sudo()
        existing = WorkOrder.search([("production_id", "in", productions.ids)]).production_id

        vals_list = []
        for production in productions:
            product = production.product_id
            if (
                production in existing
                or production.state not in ("confirmed", "progress", "to_close")
                or not product.product_tmpl_id.openmes_manufacture
                or production.uom_id.compare(production.product_qty, 0.0) <= 0
            ):
                continue
            line = production._openmes_origin_sale_line()
            if line and WorkOrder.search_count([("sale_line_id", "=", line.id), ("state", "!=", "cancelled")]):
                # Made in "sale order lines" mode before the switch: already in OpenMES.
                production.message_post(
                    body=_(
                        "No OpenMES work order was created: sale order line %(line)s already has one.",
                        line=line.display_name,
                    )
                )
                continue
            name = f"{prefix}-{production.name.replace('/', '-')}"[:100]
            if name in names or WorkOrder.search_count([("name", "=", name)]):
                suffix = f"-{production.id}"
                name = name[: 100 - len(suffix)] + suffix
            names.add(name)
            vals_list.append(
                {
                    "name": name,
                    "source_type": "production",
                    "production_id": production.id,
                    "origin_sale_order_id": production._openmes_origin_sale_order().id,
                    "product_id": product.id,
                    "line_code": product.product_tmpl_id._openmes_line_code_for(company),
                    "planned_qty": production._openmes_planned_qty(),
                }
            )
        return vals_list
