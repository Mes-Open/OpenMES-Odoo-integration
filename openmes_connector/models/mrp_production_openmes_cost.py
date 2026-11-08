"""OpenMES labour and additional costs in the manufacturing order cost.

``mrp_account`` values the finished product once, in ``_post_inventory``
(``_cal_price``): consumed components + Odoo work centre cost +
``extra_cost`` × produced quantity. Changing ``extra_cost`` after the close
changes nothing in the valuation, so the cost is set right before
``_post_inventory``, i.e. inside the close itself. The settlement is fetched
there, synchronously, when it is not stored yet; if that fails, the MO is
closed as before and the chatter says the cost was not added.

``_post_inventory`` (not ``button_mark_done``) because it only runs when the
MO really closes: after the consumption / backorder wizards and after the
backorder split, so ``qty_producing`` is the quantity being closed.
"""

from odoo import _, models
from odoo.tools import float_compare

MES_DONE = "DONE"


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    def _openmes_has_extra_cost(self):
        """``mrp_account`` (Extra Unit Cost) is installed. No hard dependency."""
        return "extra_cost" in self._fields

    def _post_inventory(self, *args, **kwargs):
        self._openmes_apply_extra_cost()
        return super()._post_inventory(*args, **kwargs)

    def _openmes_apply_extra_cost(self):
        """Set ``extra_cost`` from the OpenMES settlement on MOs being closed."""
        if not self._openmes_has_extra_cost():
            return
        for production in self:
            if production.state in ("done", "cancel"):
                continue
            company = production.company_id.sudo()
            if not company.openmes_enabled or not company.openmes_mo_extra_cost:
                continue
            work_orders = production.sudo().openmes_work_order_ids.filtered(
                lambda w: w.source_type == "production" and w.state != "cancelled"
            )
            if not work_orders:
                # Not in OpenMES (made before the mode switch, or sale lines mode).
                continue
            production._openmes_apply_extra_cost_from(work_orders)

    def _openmes_extra_cost_not_added(self, reason):
        self.ensure_one()
        self.sudo().message_post(
            body=_(
                "OpenMES labour and additional costs were not added to the manufacturing order cost: %(reason)s",
                reason=reason,
            )
        )

    def _openmes_apply_extra_cost_from(self, work_orders):
        self.ensure_one()
        done = work_orders.filtered(lambda w: w.mes_status == MES_DONE)
        if not done:
            self._openmes_extra_cost_not_added(
                _("OpenMES work order %(order)s is not finished yet.", order=", ".join(work_orders.mapped("name")))
            )
            return
        if len(done) > 1:
            self._openmes_extra_cost_not_added(
                _(
                    "several OpenMES work orders (%(orders)s) are finished for this manufacturing order.",
                    orders=", ".join(done.mapped("name")),
                )
            )
            return
        work_order = done
        if work_order.settlement_state != "done":
            # Never raises: a failure is stored on the work order.
            work_order._openmes_fetch_settlement()
        if work_order.settlement_state != "done":
            self._openmes_extra_cost_not_added(
                _(
                    "the settlement of OpenMES work order %(order)s could not be fetched (%(error)s). "
                    "The manufacturing order was closed without them.",
                    order=work_order.name,
                    error=work_order.settlement_error or _("unknown error"),
                )
            )
            return
        company_currency = self.company_id.currency_id
        if work_order.settlement_mixed_currency:
            self._openmes_extra_cost_not_added(
                _("OpenMES summed costs in several currencies without conversion.")
            )
            return
        if work_order.settlement_currency_id != company_currency:
            self._openmes_extra_cost_not_added(
                _(
                    "the settlement is in %(currency)s, the company currency is %(company_currency)s. "
                    "Amounts are never converted.",
                    currency=work_order.settlement_currency_code or _("no currency"),
                    company_currency=company_currency.name,
                )
            )
            return
        # The quantity of the finished move that _cal_price multiplies
        # extra_cost by, in the product's unit of measure.
        qty = self.uom_id._compute_quantity(
            self.qty_producing - self.qty_produced, self.product_id.uom_id, round=False
        )
        if self.product_id.uom_id.compare(qty, 0.0) <= 0:
            return
        labor = work_order.settlement_labor_cost
        additional = work_order.settlement_additional_cost
        unit = (labor + additional) / qty
        old = self.extra_cost
        self.extra_cost = unit
        text = _(
            "OpenMES labour (%(labor)s %(currency)s) and additional costs (%(additional)s %(currency)s) of "
            "work order %(order)s were added to the manufacturing order cost: %(unit)s %(currency)s per unit "
            "(Extra Unit Cost). Material costs from OpenMES are not added: Odoo values the components itself.",
            labor="%.2f" % labor,
            additional="%.2f" % additional,
            order=work_order.name,
            unit="%.4f" % unit,
            currency=company_currency.name,
        )
        if old and float_compare(old, unit, precision_digits=6):
            text += " " + _("It replaces the previous Extra Unit Cost of %(old)s.", old="%.4f" % old)
        self.sudo().message_post(body=text)
