from odoo import api, fields, models

from .openmes_client import DEFAULT_ORDER_PREFIX, SOURCE_SALE_LINES


class SaleOrder(models.Model):
    _inherit = "sale.order"

    openmes_work_order_ids = fields.One2many("openmes.work.order", "sale_order_id", string="OpenMES Work Orders")
    openmes_work_order_count = fields.Integer(compute="_compute_openmes_work_order_count")

    def _openmes_work_order_domain(self):
        # Work orders from the order's lines and from MOs made for the order.
        return ["|", ("sale_order_id", "in", self.ids), ("origin_sale_order_id", "in", self.ids)]

    @api.depends("openmes_work_order_ids")
    def _compute_openmes_work_order_count(self):
        if not self.ids:
            self.openmes_work_order_count = 0
            return
        work_orders = self.env["openmes.work.order"].sudo().search(self._openmes_work_order_domain())
        for order in self:
            order.openmes_work_order_count = len(
                work_orders.filtered(lambda w: order in (w.sale_order_id | w.origin_sale_order_id))
            )

    def action_confirm(self):
        res = super().action_confirm()
        self._openmes_create_work_orders()
        return res

    def _action_cancel(self):
        res = super()._action_cancel()
        # Only work orders made from the lines; MO work orders follow their MO.
        self.sudo().openmes_work_order_ids._openmes_queue_cancellation()
        return res

    def write(self, vals):
        res = super().write(vals)
        if "commitment_date" in vals:
            self.sudo().openmes_work_order_ids.filtered(
                lambda w: w.sale_order_id.state == "sale"
            )._openmes_request_update({})
        return res

    def _openmes_create_work_orders(self):
        vals_list = []
        for company in self.company_id:
            # Each company decides for itself (enabled, source, prefix, line).
            vals_list += self.filtered(lambda o: o.company_id == company)._openmes_work_order_vals(company.sudo())
        if vals_list:
            self.env["openmes.work.order"].sudo().create(vals_list)
            # Send right after this transaction commits instead of making the
            # confirmation wait for (or fail on) the OpenMES round trip.
            self.env.ref("openmes_connector.ir_cron_openmes_send").sudo()._trigger()

    def _openmes_work_order_vals(self, company):
        if not company.openmes_enabled or company.openmes_order_source != SOURCE_SALE_LINES:
            return []
        prefix = company.openmes_order_prefix or DEFAULT_ORDER_PREFIX
        # Salespeople confirm orders but only read OpenMES work orders.
        WorkOrder = self.env["openmes.work.order"].sudo()
        existing_lines = WorkOrder.search([("sale_line_id", "in", self.order_line.ids)]).sale_line_id

        vals_list = []
        for line in self.order_line:
            if (
                line.display_type
                or line in existing_lines
                or not line.product_id.product_tmpl_id.openmes_manufacture
                or line.product_uom_qty <= 0
            ):
                continue
            product = line.product_id
            vals_list.append(
                {
                    "name": f"{prefix}-{line.order_id.name}-{line.id}"[:100],
                    "sale_order_id": line.order_id.id,
                    "sale_line_id": line.id,
                    "product_id": product.id,
                    "line_code": product.product_tmpl_id._openmes_line_code_for(company),
                    "planned_qty": line.product_uom_id._compute_quantity(
                        line.product_uom_qty, product.uom_id, round=False
                    ),
                }
            )
        return vals_list

    def action_view_openmes_work_orders(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("openmes_connector.action_openmes_work_order")
        action["domain"] = self._openmes_work_order_domain()
        action["context"] = {"create": False}
        return action


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    def write(self, vals):
        res = super().write(vals)
        if {"product_uom_qty", "product_uom_id", "name"} & set(vals):
            confirmed = self.filtered(lambda line: line.order_id.state == "sale")
            if confirmed:
                work_orders = self.env["openmes.work.order"].sudo().search([("sale_line_id", "in", confirmed.ids)])
                for work_order in work_orders:
                    line = work_order.sale_line_id
                    work_order._openmes_request_update(
                        {"planned_qty": line.product_uom_id._compute_quantity(
                            line.product_uom_qty, work_order.product_id.uom_id, round=False
                        )}
                    )
        return res
