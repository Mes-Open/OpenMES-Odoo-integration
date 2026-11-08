"""Settlement of a finished work order: material consumption, scrap, actual cost.

Once OpenMES reports a work order as DONE, its settlement is fetched once from
``GET /work-orders/{order_no}?include=consumption,scrap,costs`` and stored on
the work order. The fetch never runs inside the status pull or the webhook
request: the order is only marked *pending* there and a cron fetches it, so a
failed fetch never undoes the status and is retried. Nothing is booked in
accounting: the numbers are for information only.
"""

import logging
import math

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError

from .openmes_client import OpenmesApiError, uom_rounding
from .openmes_work_order import MAX_AUTO_ATTEMPTS
from .openmes_work_order_attention import _qty_str

_logger = logging.getLogger(__name__)

SETTLEMENT_SECTIONS = ("consumption", "scrap", "costs")
MAX_CONSUMPTION_LINES = 1000
CRON_BATCH = 100


def _number(value):
    """A finite float from an OpenMES number; ``None`` counts as 0."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise TypeError("boolean is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("not a finite number")
    return number


def _text(value, limit):
    if value is None or isinstance(value, (dict, list)):
        return False
    return str(value).strip()[:limit] or False


class OpenmesWorkOrderConsumption(models.Model):
    _name = "openmes.work.order.consumption"
    _description = "OpenMES Work Order Material Consumption"
    _order = "id"

    work_order_id = fields.Many2one(
        "openmes.work.order", "Work Order", required=True, readonly=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(related="work_order_id.company_id", store=True, index=True)
    material_code = fields.Char("Material Code", readonly=True)
    material_name = fields.Char("Material", readonly=True)
    product_id = fields.Many2one(
        "product.product",
        "Product",
        readonly=True,
        help="The Odoo product whose Internal Reference equals the OpenMES material code, if exactly one does.",
    )
    lot_numbers = fields.Char(
        "Lots",
        readonly=True,
        help="Lots picked for this material in OpenMES, with the picked quantity in brackets.",
    )
    consumed_qty = fields.Float("Consumed", digits="Product Unit", readonly=True)
    scrap_qty = fields.Float("Scrapped", digits="Product Unit", readonly=True)
    returned_qty = fields.Float("Returned", digits="Product Unit", readonly=True)
    unit_of_measure = fields.Char(
        "Unit", readonly=True, help="The unit of measure as OpenMES sent it; it is not converted."
    )


class OpenmesWorkOrder(models.Model):
    _inherit = "openmes.work.order"

    settlement_state = fields.Selection(
        [("pending", "Pending"), ("done", "Fetched"), ("error", "Error")],
        "Settlement",
        readonly=True,
        copy=False,
        index=True,
        help="Whether the settlement (consumption, scrap and cost) of the finished order was fetched from OpenMES.",
    )
    settlement_attempts = fields.Integer("Settlement Attempts", readonly=True, copy=False)
    settlement_error = fields.Text("Settlement Error", readonly=True, copy=False)
    settlement_date = fields.Datetime("Settlement Fetched On", readonly=True, copy=False)
    scrap_qty = fields.Float(
        "Scrap",
        digits="Product Unit",
        readonly=True,
        copy=False,
        help="Total scrap reported in OpenMES, in the unit of measure of the work order's product.",
    )
    settlement_total_cost = fields.Float(
        "Total Cost",
        digits=(16, 4),
        readonly=True,
        copy=False,
        help="Actual cost from OpenMES: materials, labour and additional costs. No machine cost.",
    )
    settlement_cost_per_unit = fields.Float(
        "Cost per Unit",
        digits=(16, 4),
        readonly=True,
        copy=False,
        help="Total cost divided by the produced quantity (0 when nothing was produced).",
    )
    settlement_material_cost = fields.Float(
        "Material Cost",
        digits=(16, 4),
        readonly=True,
        copy=False,
        help="Material cost from OpenMES. Never added to the manufacturing order cost: Odoo values the "
        "components itself.",
    )
    settlement_labor_cost = fields.Float("Labour Cost", digits=(16, 4), readonly=True, copy=False)
    settlement_additional_cost = fields.Float("Additional Costs", digits=(16, 4), readonly=True, copy=False)
    settlement_currency_code = fields.Char(
        "Cost Currency", size=3, readonly=True, copy=False, help="The currency code as OpenMES sent it."
    )
    settlement_currency_id = fields.Many2one(
        "res.currency",
        "Cost Currency in Odoo",
        readonly=True,
        copy=False,
        help="The Odoo currency with the OpenMES code; empty when Odoo has none. Amounts are never converted.",
    )
    settlement_mixed_currency = fields.Boolean(
        "Mixed Currencies",
        readonly=True,
        copy=False,
        help="Some cost items are in another currency; OpenMES summed them without conversion.",
    )
    consumption_ids = fields.One2many(
        "openmes.work.order.consumption", "work_order_id", "Material Consumption", readonly=True, copy=False
    )

    # ── Hook into the pull and the webhook ───────────────────────────────────

    def _openmes_apply_status(self, row):
        before = self.mes_status
        # super() first: closing the MO and the quantity activity run before.
        result = super()._openmes_apply_status(row)
        if before != "DONE" and self.mes_status == "DONE" and self.settlement_state not in ("pending", "done"):
            # No HTTP here: the webhook answers at once and a failed fetch
            # never rolls back the status. The cron fetches it.
            self.write({"settlement_state": "pending", "settlement_attempts": 0, "settlement_error": False})
            self.env.ref("openmes_connector.ir_cron_openmes_settlements").sudo()._trigger()
        return result

    # ── Fetching ─────────────────────────────────────────────────────────────

    @api.model
    def _cron_openmes_fetch_settlements(self):
        self.env["res.company"]._openmes_cron_each(
            "settlement fetch",
            lambda company: self.sudo().with_company(company)._openmes_cron_fetch_settlements(company),
        )

    @api.model
    def _openmes_cron_fetch_settlements(self, company):
        records = self.search(
            [
                ("company_id", "=", company.id),
                ("settlement_state", "in", ("pending", "error")),
                ("settlement_attempts", "<", MAX_AUTO_ATTEMPTS),
                ("mes_status", "=", "DONE"),
            ],
            limit=CRON_BATCH,
            order="id",
        )
        records._openmes_fetch_settlement()

    def action_openmes_fetch_settlement(self):
        """Fetch the settlement now (after failed retries, or for orders
        finished before the module could fetch settlements)."""
        self.check_access("write")
        todo = self.filtered(lambda r: r.mes_status == "DONE" and r.settlement_state != "done")
        if not todo:
            raise UserError(_("Only work orders done in OpenMES and not settled yet can fetch a settlement."))
        todo.write({"settlement_attempts": 0})
        todo._openmes_fetch_settlement()
        failed = todo.filtered(lambda r: r.settlement_state != "done")
        if failed:
            return self._notify(
                _("The settlement of %(count)s work order(s) could not be fetched. See the error on each order.",
                  count=len(failed)),
                "warning",
            )
        return self._notify(_("Settlement fetched from OpenMES."), "success")

    def _openmes_settlement_failed(self, message):
        self.ensure_one()
        self.write(
            {
                "settlement_state": "error",
                "settlement_error": message,
                "settlement_attempts": self.settlement_attempts + 1,
            }
        )

    def _openmes_fetch_settlement(self):
        """Fetch and store the settlement of each order. Never raises."""
        for record in self:
            client = self.env["openmes.client"].sudo().with_company(record.company_id or self.env.company)
            try:
                data = client._get_work_order_settlement(record.name)
            except OpenmesApiError as exc:
                record._openmes_settlement_failed(str(exc))
                continue
            if not any(key in data for key in SETTLEMENT_SECTIONS):
                record._openmes_settlement_failed(
                    _("OpenMES did not return settlement data; update OpenMES to a version that supports it.")
                )
                continue
            if data.get("status") != "DONE":
                record._openmes_settlement_failed(_("OpenMES no longer reports the work order as done."))
                continue
            try:
                vals, lines = record._openmes_parse_settlement(data)
                with self.env.cr.savepoint():
                    record.sudo().write(
                        dict(
                            vals,
                            consumption_ids=[Command.clear()] + [Command.create(line) for line in lines],
                            settlement_state="done",
                            settlement_date=fields.Datetime.now(),
                            settlement_error=False,
                            settlement_attempts=0,
                        )
                    )
            except (ValueError, TypeError, OverflowError, AttributeError):
                _logger.warning("OpenMES settlement of work order %s carried invalid values", record.id)
                self.env.invalidate_all()
                record._openmes_settlement_failed(_("OpenMES returned invalid settlement data."))
                continue
            record._post_on_source(record._openmes_settlement_summary())

    def _openmes_parse_settlement(self, data):
        """Values for the work order and its consumption lines.

        Raises ValueError / TypeError on malformed numbers, so that nothing
        partial is stored.
        """
        self.ensure_one()
        scrap = data.get("scrap") or {}
        costs = data.get("costs") or {}
        consumption = data.get("consumption") or []
        if not isinstance(scrap, dict) or not isinstance(costs, dict) or not isinstance(consumption, list):
            raise ValueError("unexpected settlement section")
        code = _text(costs.get("currency"), 3)
        code = code.upper() if code else False
        currency = (
            self.env["res.currency"].sudo().with_context(active_test=False).search([("name", "=", code)], limit=1)
            if code
            else self.env["res.currency"]
        )
        sections = {}
        for key in ("materials", "labor", "additional"):
            section = costs.get(key)
            if section is None:
                section = {}
            if not isinstance(section, dict):
                raise ValueError("unexpected cost section")
            sections[key] = _number(section.get("total"))
        vals = {
            "scrap_qty": _number(scrap.get("total_qty")),
            "settlement_total_cost": _number(costs.get("total_cost")),
            "settlement_material_cost": sections["materials"],
            "settlement_labor_cost": sections["labor"],
            "settlement_additional_cost": sections["additional"],
            "settlement_cost_per_unit": _number(costs.get("cost_per_unit")),
            "settlement_currency_code": code,
            "settlement_currency_id": currency.id or False,
            "settlement_mixed_currency": bool(costs.get("mixed_currency")),
        }
        consumption = consumption[:MAX_CONSUMPTION_LINES]
        products = self._openmes_settlement_products(
            {_text(item.get("material_code"), 255) for item in consumption if isinstance(item, dict)}
        )
        lines = []
        for item in consumption:
            if not isinstance(item, dict):
                raise ValueError("unexpected consumption row")
            lots = []
            for lot in item.get("lots") or []:
                if not isinstance(lot, dict):
                    raise ValueError("unexpected lot row")
                number = _text(lot.get("lot_number"), 255)
                if not number:
                    continue
                quantity = lot.get("quantity")
                lots.append(f"{number} ({_qty_str(_number(quantity), 0.0001)})" if quantity is not None else number)
            material_code = _text(item.get("material_code"), 255)
            lines.append(
                {
                    "material_code": material_code,
                    "material_name": _text(item.get("material_name"), 255),
                    "product_id": products.get(material_code, False),
                    "lot_numbers": ", ".join(lots)[:1000] or False,
                    "consumed_qty": _number(item.get("consumed_qty")),
                    "scrap_qty": _number(item.get("scrap_qty")),
                    "returned_qty": _number(item.get("returned_qty")),
                    "unit_of_measure": _text(item.get("unit_of_measure"), 64),
                }
            )
        return vals, lines

    def _openmes_settlement_products(self, codes):
        """``{material code: product id}`` for the codes that name exactly one
        product of this company (or a shared one); one search for all lines."""
        codes = [code for code in codes if code]
        if not codes:
            return {}
        products = (
            self.env["product.product"]
            .sudo()
            .search_read(
                [("default_code", "in", codes), ("company_id", "in", [False, self.company_id.id])],
                ["default_code"],
            )
        )
        found = {}
        for product in products:
            found.setdefault(product["default_code"], []).append(product["id"])
        return {code: ids[0] for code, ids in found.items() if len(ids) == 1}

    def _openmes_settlement_summary(self):
        self.ensure_one()
        currency = self.settlement_currency_code or ""
        text = _(
            "Settlement of OpenMES work order %(order)s: scrap %(scrap)s %(uom)s, total cost %(total)s "
            "%(currency)s (%(unit)s %(currency)s per unit), %(count)s material(s) consumed.",
            order=self.name,
            scrap=_qty_str(self.scrap_qty, uom_rounding(self.env)),
            uom=self.product_uom_id.name,
            total="%.2f" % self.settlement_total_cost,
            unit="%.4f" % self.settlement_cost_per_unit,
            currency=currency,
            count=len(self.consumption_ids),
        )
        if self.settlement_mixed_currency:
            text += " " + _("Some costs are in other currencies and were summed without conversion.")
        return text
