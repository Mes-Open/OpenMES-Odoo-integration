"""Master-data push from Odoo to OpenMES: products, materials, recipes, stock.

Scope: products flagged "Manufacture in OpenMES" become OpenMES product types;
the components of their bills of materials become OpenMES materials; each BoM
becomes a recipe; and the on-hand quantities of all of those, per warehouse
with an OpenMES code, are pushed as a stock snapshot. Odoo is the source of
truth for all four, so every push is an idempotent upsert.
"""

import logging
import re
from collections import OrderedDict

import pytz

from odoo import _, api, models
from odoo.tools import float_round, html2plaintext

from .openmes_client import OpenmesApiError

_logger = logging.getLogger(__name__)

EXTERNAL_SYSTEM = "odoo"
# Per-request row limits of the OpenMES import endpoints.
CHUNK_PRODUCTS = 2000
CHUNK_MATERIALS = 2000
CHUNK_BOMS = 500
CHUNK_ROUTINGS = 500
MAX_ROUTING_STEPS = 200
# steps.*.workstation_code / steps.*.line_code of /process-templates/import.
MAX_STEP_CODE = 50
STEP_FIELD_RE = re.compile(r"^steps\.(\d+)\.(workstation_code|line_code)$")
QUOTED_RE = re.compile(r"'([^']+)'")
CHUNK_STOCK = 5000
CHUNK_LOTS = 5000
# A 0 OpenMES keeps refusing is given up after this many runs.
MAX_ZERO_REFUSALS = 3
MAX_BOM_COMPONENTS = 200
# product.template.tracking → OpenMES materials.tracking_type
TRACKING_MAP = {"none": "none", "lot": "batch", "serial": "serial"}
MAX_LOGGED_ERRORS = 200


class OpenmesSync(models.AbstractModel):
    _name = "openmes.sync"
    _description = "OpenMES master data synchronisation"

    # ── Scope ────────────────────────────────────────────────────────────────

    # Everything runs for ``self.env.company``: its products (and the shared
    # ones), its BoMs and its warehouses go to its own OpenMES instance.

    @api.model
    def _manufactured_products(self):
        return self.env["product.product"].search([
            ("product_tmpl_id.openmes_manufacture", "=", True),
            ("company_id", "in", [False, self.env.company.id]),
        ])

    @api.model
    def _boms_by_product(self, products):
        boms = self.env["mrp.bom"]._bom_find(products, company_id=self.env.company.id, bom_type="normal")
        return {product: bom for product, bom in boms.items() if bom}

    @api.model
    def _bom_components(self, product, bom):
        """Return {component product: quantity per ONE unit of ``product``}."""
        bom_qty = bom.product_uom_id._compute_quantity(bom.product_qty, product.uom_id, round=False)
        components = OrderedDict()
        if not bom_qty:
            return components
        for line in bom.bom_line_ids:
            if line._skip_bom_line(product):
                continue
            component = line.product_id
            qty = line.product_uom_id._compute_quantity(line.product_qty, component.uom_id, round=False)
            components[component] = components.get(component, 0.0) + qty / bom_qty
        return components

    @api.model
    def _materials(self, boms_by_product):
        materials = self.env["product.product"]
        for product, bom in boms_by_product.items():
            for component in self._bom_components(product, bom):
                materials |= component
        return materials

    # ── Payloads ─────────────────────────────────────────────────────────────

    @api.model
    def _item_name(self, product):
        return product.with_context(display_default_code=False).display_name[:255]

    @api.model
    def _product_row(self, product):
        return {
            "code": product.default_code,
            "name": self._item_name(product),
            "description": (product.description_sale or "")[:2000] or None,
            "category": product.categ_id.name[:100] if product.categ_id else None,
            "unit_of_measure": product.uom_id.name[:20],
            "external_code": str(product.id),
            "external_system": EXTERNAL_SYSTEM,
            "is_active": bool(product.active),
        }

    @api.model
    def _material_row(self, product):
        # The cost is per company.
        product = product.with_company(self.env.company)
        row = {
            "code": product.default_code,
            "name": self._item_name(product),
            "category": product.categ_id.name[:100] if product.categ_id else None,
            "unit_of_measure": product.uom_id.name[:20],
            "tracking_type": TRACKING_MAP.get(product.tracking, "none"),
            "external_code": str(product.id),
            "external_system": EXTERNAL_SYSTEM,
            "is_active": bool(product.active),
        }
        if product.standard_price:
            row["unit_price"] = max(0.0, float_round(product.standard_price, precision_digits=4))
            row["price_currency"] = (product.cost_currency_id or self.env.company.currency_id).name
        return row

    @api.model
    def _split_coded(self, products, label):
        """Split ``products`` into (rows with a code, local error messages)."""
        coded = products.filtered("default_code")
        errors = [
            _("%(kind)s %(product)s skipped: no Internal Reference.", kind=label, product=p.display_name)
            for p in products - coded
        ]
        return coded, errors

    # ── Runner ───────────────────────────────────────────────────────────────

    @api.model
    def _run_import(
        self,
        operation,
        path,
        list_key,
        rows,
        chunk_size,
        envelope=None,
        local_errors=None,
        explain_error=None,
        row_label=None,
        on_report=None,
        error_prefix="",
    ):
        """Send ``rows`` in chunks and record the outcome in openmes.sync.log.

        ``explain_error(source_row, error)`` may return text appended to the
        logged line of a row error (e.g. the Odoo record behind a code).
        ``row_label(source_row)`` names a row in the log (default: its code).
        ``on_report(part, failed_rows)`` is called for every chunk OpenMES
        answered, with the indexes (0-based, in ``part``) of the failed rows,
        or None when an error names no usable row (it is unknown which rows
        failed); it may return extra lines for the log.
        ``error_prefix`` starts every line logged by this call."""
        client = self.env["openmes.client"].sudo().with_company(self.env.company)
        totals = {"imported": 0, "updated": 0, "skipped": 0}
        errors = list(local_errors or [])
        failed = False

        for offset in range(0, len(rows), chunk_size):
            part = rows[offset : offset + chunk_size]
            body = dict(envelope or {}, **{list_key: part})
            try:
                report = client._post_import(path, body)
            except OpenmesApiError as exc:
                errors.append(f"{error_prefix}{exc}")
                failed = True
                break
            for key in totals:
                totals[key] += report[key]
            failed_rows = set()
            for error in report["errors"]:
                row_no = error.get("row")
                valid = isinstance(row_no, int) and 0 < row_no <= len(part)
                source = part[row_no - 1] if valid else {}
                if valid:
                    failed_rows.add(row_no - 1)
                if row_label and source:
                    label = row_label(source)
                else:
                    label = (
                        source.get("code") or source.get("product_type_code") or source.get("material_code") or "?"
                    )
                field = error.get("field")
                message = error.get("message") or _("Unknown error")
                hint = explain_error(source, error) if explain_error else None
                errors.append(f"{error_prefix}{label}: {field + ': ' if field else ''}{message}{hint or ''}")
            if on_report:
                if any(not isinstance(e.get("row"), int) or not 0 < e["row"] <= len(part) for e in report["errors"]):
                    failed_rows = None
                errors.extend(on_report(part, failed_rows) or [])

        if failed and not any(totals.values()):
            state = "failed"
        elif errors:
            state = "partial"
        else:
            state = "success"
        message = "\n".join(errors[:MAX_LOGGED_ERRORS])
        if len(errors) > MAX_LOGGED_ERRORS:
            message += "\n" + _("… and %s more.", len(errors) - MAX_LOGGED_ERRORS)

        return self.env["openmes.sync.log"].sudo().create(
            {
                "company_id": self.env.company.id,
                "operation": operation,
                "state": state,
                "sent_count": len(rows),
                "imported_count": totals["imported"],
                "updated_count": totals["updated"],
                "skipped_count": totals["skipped"],
                "error_count": len(errors),
                "message": message or False,
            }
        )

    # ── Operations ───────────────────────────────────────────────────────────

    @api.model
    def _sync_products(self, products, create_default_template=False):
        """Push products. With ``create_default_template`` OpenMES gives a
        product type that has no process template a one-step one, so orders
        for it are accepted before anyone models its routing."""
        coded, local_errors = self._split_coded(products, _("Product"))
        rows = [self._product_row(p) for p in coded]
        envelope = {"strategy": "update_or_create", "external_system": EXTERNAL_SYSTEM}
        if create_default_template:
            envelope["create_default_template"] = True
        return self._run_import(
            "products",
            "/products/import",
            "products",
            rows,
            CHUNK_PRODUCTS,
            envelope=envelope,
            local_errors=local_errors,
        )

    @api.model
    def _routing_operations(self, product, bom):
        return bom.operation_ids.filtered(lambda op: not op._skip_operation_line(product)).sorted(
            lambda op: (op.sequence, op.id)
        )

    @api.model
    def _openmes_workstation_code(self, workcenter):
        """Workstation code of ``workcenter`` for this company's OpenMES.

        A shared BoM can use another company's work center; its code names a
        workstation in that company's OpenMES, so it is not sent here."""
        workcenter = workcenter.sudo()
        if workcenter.company_id and workcenter.company_id != self.env.company:
            return False
        return workcenter.openmes_workstation_code or False

    @api.model
    def _routing_row(self, product, bom):
        line_code = product.product_tmpl_id._openmes_line_code_for(self.env.company)
        if line_code and len(line_code) > MAX_STEP_CODE:
            line_code = False
        steps = []
        for index, operation in enumerate(self._routing_operations(product, bom)[:MAX_ROUTING_STEPS], start=1):
            capacity = operation.workcenter_id.default_capacity or 1.0
            step = {
                "step_number": index * 10,
                "name": (operation.name or _("Operation %s", index))[:255],
                # Odoo times one cycle of the work center; OpenMES wants minutes per unit.
                "run_time_per_unit_minutes": float_round(
                    (operation.time_cycle_manual or 0.0) / capacity, precision_digits=4
                ),
            }
            if operation.note:
                step["instruction"] = html2plaintext(operation.note)[:10000] or None
            # Without a code the planner picks the workstation in OpenMES.
            # A changed code changes the routing, so the next sync makes a
            # new version; an unchanged one is skipped by OpenMES.
            workstation_code = self._openmes_workstation_code(operation.workcenter_id)
            if workstation_code:
                step["workstation_code"] = workstation_code
                # The line the product's work orders go to: narrows the
                # lookup when OpenMES has the code on several lines.
                if line_code:
                    step["line_code"] = line_code
            steps.append(step)
        return {
            "product_type_code": product.default_code,
            "template_name": bom.display_name[:255],
            "steps": steps,
        }

    @api.model
    def _sync_routings(self, boms_by_product):
        rows = []
        names_by_code = {}
        for product, bom in boms_by_product.items():
            operations = self._routing_operations(product, bom)
            if not (product.default_code and operations):
                continue
            rows.append(self._routing_row(product, bom))
            for workcenter in operations[:MAX_ROUTING_STEPS].workcenter_id.sudo():
                code = self._openmes_workstation_code(workcenter)
                if code:
                    names_by_code.setdefault(code, set()).add(workcenter.display_name)

        def explain_error(source, error):
            """Name the Odoo work center behind a workstation/line error."""
            match = STEP_FIELD_RE.match(error.get("field") or "")
            if not match:
                return None
            steps = source.get("steps") or []
            row_codes = {step.get("workstation_code") for step in steps} - {None}
            # The code quoted in the message ("Workstation 'WS-1' not found"),
            # else the step the field points at. That fallback assumes the
            # step index is 0-based (Laravel's ``steps.N`` validation keys,
            # i.e. a position in the ``steps`` array we sent); the API docs do
            # not say, so a line error could name the next step's work center
            # if OpenMES ever counted from 1.
            code = next(
                (c for c in QUOTED_RE.findall(error.get("message") or "") if c in row_codes), None
            )
            index = int(match.group(1))
            if not code and index < len(steps):
                code = steps[index].get("workstation_code")
            if not code or code not in names_by_code:
                return None
            return self.env._(" (Odoo work center: %(names)s)", names=", ".join(sorted(names_by_code[code])))

        return self._run_import(
            "routings",
            "/process-templates/import",
            "templates",
            rows,
            CHUNK_ROUTINGS,
            # A new active version; orders already in production keep the
            # snapshot of the version they started with.
            envelope={"mode": "new_version"},
            explain_error=explain_error,
        )

    @api.model
    def _sync_materials(self, materials):
        coded, local_errors = self._split_coded(materials, _("Component"))
        rows = [self._material_row(p) for p in coded]
        return self._run_import(
            "materials",
            "/materials/import",
            "materials",
            rows,
            CHUNK_MATERIALS,
            envelope={"strategy": "update_or_create", "external_system": EXTERNAL_SYSTEM},
            local_errors=local_errors,
        )

    @api.model
    def _sync_boms(self, boms_by_product):
        rows, local_errors = [], []
        for product, bom in boms_by_product.items():
            if not product.default_code:
                continue  # already reported by the product sync
            components = self._bom_components(product, bom)
            uncoded = [c.display_name for c in components if not c.default_code]
            if uncoded:
                local_errors.append(
                    _(
                        "%(product)s: recipe skipped, components without Internal Reference: %(items)s",
                        product=product.default_code,
                        items=", ".join(uncoded),
                    )
                )
                continue
            lines = [
                {"material_code": c.default_code, "quantity_per_unit": float_round(qty, precision_digits=6)}
                for c, qty in components.items()
                if qty > 0
            ]
            if not lines:
                continue
            if len(lines) > MAX_BOM_COMPONENTS:
                local_errors.append(
                    _(
                        "%(product)s: recipe skipped, more than %(max)s components.",
                        product=product.default_code,
                        max=MAX_BOM_COMPONENTS,
                    )
                )
                continue
            rows.append({"product_type_code": product.default_code, "components": lines})
        return self._run_import(
            "boms",
            "/boms/import",
            "recipes",
            rows,
            CHUNK_BOMS,
            envelope={"mode": "replace"},
            local_errors=local_errors,
        )

    @api.model
    def _stock_rows(self, warehouse, products, materials):
        items = (products | materials).filtered("default_code")
        groups = self.env["stock.quant"]._read_group(
            [("location_id", "child_of", warehouse.lot_stock_id.id), ("product_id", "in", items.ids)],
            ["product_id"],
            ["quantity:sum"],
        )
        on_hand = {product: qty for product, qty in groups}
        rows = []
        # Zero rows are sent on purpose: the import is a snapshot, so an item
        # that ran out in Odoo must also drop to zero in OpenMES.
        for kind, records in (("product_type_code", products), ("material_code", materials)):
            for product in records.filtered("default_code"):
                rows.append(
                    {
                        kind: product.default_code,
                        "quantity": max(0.0, float_round(on_hand.get(product, 0.0), precision_digits=4)),
                        "unit_of_measure": product.uom_id.name[:20],
                    }
                )
        return rows

    # ── Material lots ────────────────────────────────────────────────────────

    @api.model
    def _lot_time_zone(self, warehouse):
        """Time zone of the expiry dates of ``warehouse``: its address's, else
        the company's, else UTC — never the user's, so the cron and the
        button send the same date."""
        name = warehouse.sudo().partner_id.tz or self.env.company.partner_id.tz or "UTC"
        try:
            return pytz.timezone(name)
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    @api.model
    def _lot_expiry(self, lot, tz=pytz.utc):
        """Expiry date of ``lot`` in ``tz`` as an ISO date, or None.

        ``expiration_date`` comes with the optional ``product_expiry`` module,
        which this module does not depend on."""
        if "expiration_date" not in lot._fields or not lot.expiration_date:
            return None
        return pytz.utc.localize(lot.expiration_date).astimezone(tz).date().isoformat()

    @api.model
    def _lot_rows(self, warehouse, materials):
        """Lots of the lot-tracked ``materials`` on hand in ``warehouse``,
        plus a 0 for every lot sent before that is no longer on hand.

        Returns (rows, {(material_code, lot_number): stock.lot}) for the rows
        with a quantity."""
        tracked = materials.filtered(lambda p: p.tracking == "lot" and p.default_code)
        rows, lots_by_key = [], {}
        tz = self._lot_time_zone(warehouse)
        if tracked:
            groups = self.env["stock.quant"]._read_group(
                [
                    ("location_id", "child_of", warehouse.lot_stock_id.id),
                    ("product_id", "in", tracked.ids),
                    ("lot_id", "!=", False),
                ],
                ["product_id", "lot_id"],
                ["quantity:sum"],
            )
            for product, lot, qty in sorted(groups, key=lambda g: (g[0].default_code, g[1].name)):
                qty = float_round(qty, precision_digits=4)
                key = (product.default_code, lot.name)
                if qty <= 0 or key in lots_by_key:
                    # A negative lot is not stock (a remembered one gets its 0
                    # below); a second product with the same code is ambiguous.
                    continue
                row = {
                    "material_code": product.default_code,
                    "lot_number": lot.name,
                    "quantity_available": qty,
                    "unit_of_measure": product.uom_id.name[:20],
                }
                expiry = self._lot_expiry(lot, tz)
                if expiry:
                    row["expiry_date"] = expiry
                if lot.ref:
                    row["supplier_lot_no"] = lot.ref
                rows.append(row)
                lots_by_key[key] = lot
        for sent in self._lots_sent(warehouse):
            if (sent.material_code, sent.lot_number) not in lots_by_key:
                # No unit: a 0 is a 0 in any unit, and a unit changed in
                # OpenMES meanwhile must not make it fail.
                rows.append(
                    {"material_code": sent.material_code, "lot_number": sent.lot_number, "quantity_available": 0.0}
                )
        return rows, lots_by_key

    @api.model
    def _lots_sent(self, warehouse):
        return self.env["openmes.material.lot.sent"].sudo().search(
            [("warehouse_id", "=", warehouse.id), ("company_id", "=", self.env.company.id)]
        )

    @api.model
    def _sync_material_lots(self, warehouse, materials):
        """Push the lot balances of ``warehouse``; None when there is nothing to send.

        A lot with a quantity is remembered BEFORE it is sent (a timeout may
        still have written it in OpenMES); a 0 is forgotten once OpenMES
        accepted it. A refused 0 is sent again by the next syncs and given up
        after MAX_ZERO_REFUSALS refusals, with a line in the log."""
        rows, lots_by_key = self._lot_rows(warehouse, materials)
        if not rows:
            return None
        Sent = self.env["openmes.material.lot.sent"].sudo()
        sent_by_key = {(s.material_code, s.lot_number): s for s in self._lots_sent(warehouse)}
        new_keys = set()
        for row in rows:
            key = (row["material_code"], row["lot_number"])
            if not row["quantity_available"]:
                continue
            lot = lots_by_key[key]
            vals = {
                "unit_of_measure": row.get("unit_of_measure"),
                "product_id": lot.product_id.id,
                "lot_id": lot.id,
                "zero_refusals": 0,
            }
            if key in sent_by_key:
                sent_by_key[key].write(vals)
            else:
                sent_by_key[key] = Sent.create(
                    dict(
                        vals,
                        company_id=self.env.company.id,
                        warehouse_id=warehouse.id,
                        material_code=key[0],
                        lot_number=key[1],
                    )
                )
                new_keys.add(key)

        def on_report(part, failed_rows):
            if failed_rows is None:
                # Which rows failed is unknown: keep every remembered lot, so
                # none of them misses its 0 later.
                return []
            done, notes = Sent, []
            for index, row in enumerate(part):
                key = (row["material_code"], row["lot_number"])
                sent = sent_by_key[key]
                if row["quantity_available"]:
                    if index in failed_rows and key in new_keys:
                        # Refused, so OpenMES does not hold it: no 0 to send later.
                        done |= sent
                elif index not in failed_rows:
                    done |= sent
                elif sent.zero_refusals + 1 >= MAX_ZERO_REFUSALS:
                    done |= sent
                    notes.append(
                        self.env._(
                            "[%(warehouse)s] %(material)s / %(lot)s: the 0 was refused %(count)s times and is "
                            "no longer sent; set the lot to 0 in OpenMES.",
                            warehouse=warehouse.openmes_code,
                            material=key[0],
                            lot=key[1],
                            count=MAX_ZERO_REFUSALS,
                        )
                    )
                else:
                    sent.zero_refusals += 1
            done.unlink()
            return notes

        return self._run_import(
            "material_lots",
            "/material-lots/import",
            "lots",
            rows,
            CHUNK_LOTS,
            envelope={"warehouse_code": warehouse.openmes_code},
            row_label=lambda row: f"{row['material_code']} / {row['lot_number']}",
            on_report=on_report,
            error_prefix=f"[{warehouse.openmes_code}] ",
        )

    @api.model
    def _stock_blocked_log(self, warehouse, message):
        _logger.info("OpenMES stock snapshot of warehouse %s not sent: %s", warehouse.openmes_code, message)
        return self.env["openmes.sync.log"].sudo().create(
            {
                "company_id": self.env.company.id,
                "operation": "stock",
                "state": "failed",
                "error_count": 1,
                "message": message,
            }
        )

    @api.model
    def _sync_stock(self, products, materials):
        logs = lot_logs = self.env["openmes.sync.log"]
        warehouses = self._openmes_warehouses()
        Document = self.env["openmes.stock.document"]
        for warehouse in warehouses:
            # A snapshot replaces the OpenMES balance: sending it before an
            # OpenMES document is booked here would erase that consumption.
            blocking = Document._openmes_blocking(warehouse)
            if blocking:
                names = blocking.mapped("name")
                listed = ", ".join(names[:20]) + (" …" if len(names) > 20 else "")
                logs |= self._stock_blocked_log(
                    warehouse,
                    _(
                        "Stock snapshot of warehouse %(warehouse)s not sent: %(count)s OpenMES stock "
                        "document(s) are not booked in Odoo yet (%(documents)s). Fix them under "
                        "OpenMES > Stock Documents.",
                        warehouse=warehouse.openmes_code,
                        count=len(names),
                        documents=listed,
                    ),
                )
                continue
            # Lots first: a lot import recomputes the OpenMES warehouse total
            # from the lots, the snapshot then sets it to Odoo's on-hand.
            lot_log = self._sync_material_lots(warehouse, materials)
            if lot_log:
                lot_logs |= lot_log
                logs |= lot_log
            rows = self._stock_rows(warehouse, products, materials)
            if rows:
                logs |= self._run_import(
                    "stock",
                    "/stock/import",
                    "balances",
                    rows,
                    CHUNK_STOCK,
                    envelope={"warehouse_code": warehouse.openmes_code},
                )
        # One "Material lots" entry per run; its lines name the warehouse.
        merged = self._merge_logs(lot_logs)
        return logs - lot_logs + merged

    @api.model
    def _sync_master_data(self):
        """Products → routings → materials → recipes, in dependency order."""
        products = self._manufactured_products()
        boms = self._boms_by_product(products)
        materials = self._materials(boms)
        logs = self.env["openmes.sync.log"]
        # Products whose BoM has operations get their routing below; the rest
        # get a one-step default template so their orders are accepted.
        routed = self.env["product.product"].browse(
            [p.id for p, bom in boms.items() if self._routing_operations(p, bom)]
        )
        product_logs = self.env["openmes.sync.log"]
        if products - routed:
            product_logs |= self._sync_products(products - routed, create_default_template=True)
        if routed:
            product_logs |= self._sync_products(routed)
        logs |= self._merge_logs(product_logs)
        if routed:
            logs |= self._sync_routings(boms)
        if materials:
            logs |= self._sync_materials(materials)
        if boms:
            logs |= self._sync_boms(boms)
        return logs

    @api.model
    def _merge_logs(self, logs):
        """Fold several logs of one operation into a single entry."""
        if len(logs) < 2:
            return logs
        states = set(logs.mapped("state"))
        merged = logs[0]
        merged.write(
            {
                "state": "success" if states == {"success"} else ("failed" if states == {"failed"} else "partial"),
                "sent_count": sum(logs.mapped("sent_count")),
                "imported_count": sum(logs.mapped("imported_count")),
                "updated_count": sum(logs.mapped("updated_count")),
                "skipped_count": sum(logs.mapped("skipped_count")),
                "error_count": sum(logs.mapped("error_count")),
                "message": "\n".join(m for m in logs.mapped("message") if m) or False,
            }
        )
        (logs - merged).unlink()
        return merged

    @api.model
    def _openmes_warehouses(self):
        return self.env["stock.warehouse"].sudo().search(
            [("openmes_code", "!=", False), ("company_id", "=", self.env.company.id)]
        )

    @api.model
    def _sync_all_stock(self):
        """Book the OpenMES stock documents first, then push the snapshot of
        every warehouse that has nothing left to book."""
        document_log, backlog_read = self.env["openmes.stock.document"]._openmes_sync_documents()
        if not backlog_read:
            # Unknown backlog: any snapshot could overwrite unbooked consumption.
            logs = document_log
            if self._openmes_warehouses():
                message = _(
                    "Stock snapshot not sent: the OpenMES stock documents could not be booked first, so "
                    "unbooked consumption could be overwritten (%s). Check the scope erp:stock:read.",
                    document_log.message,
                )
                _logger.info("OpenMES stock snapshot not sent: stock documents could not be read")
                logs |= self.env["openmes.sync.log"].sudo().create(
                    {
                        "company_id": self.env.company.id,
                        "operation": "stock",
                        "state": "failed",
                        "error_count": 1,
                        "message": message,
                    }
                )
            return logs
        products = self._manufactured_products()
        materials = self._materials(self._boms_by_product(products))
        return document_log | self._sync_stock(products, materials)

    # ── Cron entry points ────────────────────────────────────────────────────

    @api.model
    def _cron_sync_master_data(self):
        self.env["res.company"]._openmes_cron_each(
            "master data sync", lambda company: self.sudo().with_company(company)._sync_master_data()
        )

    @api.model
    def _cron_sync_stock(self):
        # Commits: the stock documents are booked (and committed) first.
        self.env["res.company"]._openmes_cron_each(
            "stock sync", lambda company: self.sudo().with_company(company)._sync_all_stock(), commit=True
        )
