"""Stock documents posted in OpenMES, booked as stock moves in Odoo.

OpenMES posts a material issue for what a work order consumed and a product
receipt for what it produced (plus the reverse types). Odoo is the source of
truth for stock and pushes a snapshot that replaces the OpenMES balance, so
every posted document must be booked in Odoo before that snapshot goes out —
otherwise the consumption reported by OpenMES disappears.

Flow: fetch the ``unsynced_only`` backlog (every page first), book each new
document as done stock moves (one document = one set of moves with the
reference ``OpenMES <document_no>``), then acknowledge it with that
reference. The OpenMES document id is unique here, so a document is never
booked twice; a failed acknowledgement is only re-sent.
"""

import logging
import re

from odoo import _, api, fields, models, modules
from odoo.exceptions import AccessError, RedirectWarning, UserError

from .openmes_client import OpenmesApiError, parse_iso_datetime

_logger = logging.getLogger(__name__)

MAX_AUTO_ATTEMPTS = 5
MAX_LOGGED_ERRORS = 200
MAX_BLOCKING_LISTED = 20
# Advisory lock key shared by every run that books documents (crons, buttons),
# so two overlapping runs never book the same backlog at once.
BOOKING_LOCK_KEY = 0x6F6D5344  # "omSD"
# References sent to OpenMES are data, not UI text: never translated.
MOVE_REFERENCE = "OpenMES %s"
NOT_BOOKED_REFERENCE = "Not booked in Odoo"
MANAGER_GROUPS = ("stock.group_stock_manager", "mrp.group_mrp_manager", "base.group_system")

DOCUMENT_TYPES = [
    ("material_issue", "Material Issue"),
    ("material_receipt", "Material Return"),
    ("product_receipt", "Product Receipt"),
    ("product_issue", "Product Issue"),
]
# Types that take goods out of the warehouse (into production).
OUTGOING_TYPES = ("material_issue", "product_issue")

# Documents that are not in Odoo's stock yet: they block the stock snapshot of
# their warehouse, because the snapshot would overwrite what OpenMES consumed.
UNBOOKED_STATES = ("pending", "waiting_production", "error")

# Unit names OpenMES and Odoo use for the same unit. No conversion happens:
# a document line must be in the product's own unit, only spelled differently.
UNIT_ALIASES = [
    {"pcs", "pc", "piece", "pieces", "unit", "units", "szt", "c62", "ea", "each", "h87"},
    {"kg", "kgm", "kilogram", "kilograms", "kilogramy"},
    {"g", "grm", "gram", "grams", "gramy"},
    {"mg", "mgm", "milligram", "milligrams"},
    {"t", "tne", "ton", "tonne", "tonnes"},
    {"l", "ltr", "liter", "liters", "litre", "litres", "litry"},
    {"ml", "mlt", "milliliter", "milliliters", "millilitre", "millilitres"},
    {"m", "mtr", "meter", "meters", "metre", "metres"},
    {"cm", "cmt", "centimeter", "centimeters"},
    {"mm", "mmt", "millimeter", "millimeters"},
    {"m2", "m²", "mtk"},
    {"m3", "m³", "mtq"},
    {"dozen", "dozens", "dzn"},
]


def normalize_unit(name):
    """Normalise a unit name the way OpenMES does (trim, lower case, single
    spaces, no trailing dot)."""
    unit = re.sub(r"\s+", " ", (name or "").strip().lower())
    return unit[:-1] if unit.endswith(".") else unit


def units_match(document_unit, uom_names):
    unit = normalize_unit(document_unit)
    names = {normalize_unit(n) for n in uom_names if n}
    if unit in names:
        return True
    return any(unit in group and names & group for group in UNIT_ALIASES)


class OpenmesStockDocument(models.Model):
    _name = "openmes.stock.document"
    _description = "OpenMES Stock Document"
    _order = "posted_at desc, id desc"

    name = fields.Char("Document No.", required=True, readonly=True, index=True)
    mes_id = fields.Integer("OpenMES ID", required=True, readonly=True, index=True, copy=False)
    document_type = fields.Selection(DOCUMENT_TYPES, "Type", readonly=True)
    mes_type = fields.Char(
        "OpenMES Type", readonly=True, help="The document type exactly as OpenMES sent it."
    )
    direction = fields.Selection([("in", "In"), ("out", "Out")], readonly=True)
    posted_at = fields.Datetime("Posted", readonly=True)
    warehouse_code = fields.Char("OpenMES Warehouse", readonly=True)
    warehouse_erp_code = fields.Char("OpenMES Warehouse ERP Code", readonly=True)
    warehouse_id = fields.Many2one("stock.warehouse", "Warehouse", readonly=True, index=True)
    company_id = fields.Many2one(
        "res.company",
        "Company",
        required=True,
        readonly=True,
        index=True,
        default=lambda self: self.env.company,
        help="The company whose OpenMES instance sent the document.",
    )
    work_order_no = fields.Char("OpenMES Work Order", readonly=True, index=True)
    work_order_id = fields.Many2one("openmes.work.order", readonly=True, ondelete="set null")
    production_id = fields.Many2one("mrp.production", "Manufacturing Order", readonly=True, ondelete="set null")
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("waiting_production", "Waiting for MO"),
            ("error", "Error"),
            ("booked", "Booked"),
            ("acked", "Acknowledged"),
        ],
        default="pending",
        required=True,
        readonly=True,
        index=True,
        help="Booked: the stock moves are done in Odoo, the acknowledgement to OpenMES is "
        "still to be sent. Acknowledged: OpenMES knows the document is booked.",
    )
    handling = fields.Selection(
        [
            ("moves", "Stock moves"),
            ("production", "Manufacturing order"),
            ("manual", "Not booked"),
            ("external", "Acknowledged outside Odoo"),
        ],
        readonly=True,
        help="Stock moves: booked by this module. Manufacturing order: the MO already "
        "consumed the components and received the product, so nothing was booked "
        "twice. Not booked: acknowledged by a user without booking. Acknowledged "
        "outside Odoo: the document left the OpenMES backlog before Odoo booked it.",
    )
    move_ids = fields.One2many("stock.move", "openmes_stock_document_id", "Stock Moves", readonly=True)
    line_ids = fields.One2many("openmes.stock.document.line", "document_id", "Lines", readonly=True)
    erp_reference = fields.Char("Odoo Reference", readonly=True, help="Sent to OpenMES with the acknowledgement.")
    error_message = fields.Text("Booking Error", readonly=True)
    ack_error = fields.Text("Acknowledgement Error", readonly=True)
    attempt_count = fields.Integer("Acknowledgement Attempts", readonly=True)
    acked_at = fields.Datetime("Acknowledged On", readonly=True)

    # Same name as before: ids are unique within one OpenMES instance only.
    _mes_id_unique = models.Constraint(
        "unique(mes_id, company_id)",
        "An OpenMES stock document can only be imported once per company.",
    )

    @api.ondelete(at_uninstall=False)
    def _unlink_except_booked(self):
        """A booked document still on the OpenMES backlog would be imported
        and booked again by the next run; an acknowledged one too, if OpenMES
        ever returns it. Its record is what keeps it from being booked twice."""
        booked = self.filtered(lambda r: r.state in ("booked", "acked"))
        if booked:
            raise UserError(
                _(
                    "Stock document %s is booked or acknowledged and cannot be deleted: the record "
                    "is what keeps OpenMES from having it booked in Odoo a second time.",
                    booked[0].name,
                )
            )

    # ── Actions ──────────────────────────────────────────────────────────────

    def _openmes_check_manager(self):
        """The model is read-only for everyone but admins (its fields drive
        the snapshot block); the two actions below check the manager groups
        here and then work as superuser."""
        self.check_access("read")
        if not any(self.env.user.has_group(group) for group in MANAGER_GROUPS):
            raise AccessError(_("Only inventory or manufacturing managers can book or acknowledge OpenMES stock documents."))
        if not self._openmes_try_lock():
            raise UserError(_("Another run is booking the OpenMES stock documents right now; try again in a minute."))

    def action_retry(self):
        """Book again (after fixing the product, unit, lot…) and resend the ack.

        An unbooked document is booked only if it is still on the OpenMES
        backlog: the backlog of its company is read again first, so a document
        acknowledged in OpenMES in the meantime is never booked here."""
        self._openmes_check_manager()
        records = self.sudo().filtered(lambda r: r.state != "acked")
        records.write({"attempt_count": 0})
        unbooked = records.filtered(lambda r: r.state in UNBOOKED_STATES)
        to_book, errors = records.browse(), []
        for company in unbooked.company_id:
            still_listed, company_errors = unbooked.filtered(
                lambda r: r.company_id == company
            ).with_company(company)._openmes_refresh()
            to_book |= still_listed
            errors += company_errors
        to_book._openmes_book()
        self._openmes_commit()
        records.filtered(lambda r: r.state == "booked")._openmes_acknowledge()
        failed = records.filtered(lambda r: r.state != "acked")
        if failed or errors:
            message = _(
                "%(count)s stock document(s) are not acknowledged yet. See the message on each document.",
                count=len(failed),
            )
            return self._notify("\n".join([message] + errors), "warning")
        return self._notify(_("Stock documents booked and acknowledged."), "success")

    def _openmes_refresh(self):
        """Read the OpenMES backlog of ``self.env.company`` again for the
        unbooked documents in ``self`` (all of that company).

        Returns ``(documents that may be booked, error messages)``: the ones
        still on the backlog, with their current lines. The ones gone from it
        are marked as acknowledged elsewhere. When the backlog cannot be read
        (or one of its rows cannot), nothing missing from it may be booked and
        it is left as it is."""
        company = self.env.company
        try:
            fetched, fetch_errors = self.with_company(company)._openmes_fetch()
        except OpenmesApiError as exc:
            return self.browse(), [
                _("Stock documents of %(company)s could not be read from OpenMES: %(error)s",
                  company=company.name, error=exc)
            ]
        listed = self & fetched
        if not fetch_errors:
            (self - fetched)._openmes_mark_external()
        return listed.filtered(lambda r: r.state in UNBOOKED_STATES), list(fetch_errors)

    def _openmes_mark_external(self):
        self.write({
            "state": "acked",
            "handling": "external",
            "error_message": _("Acknowledged in OpenMES before Odoo booked it; nothing was booked in Odoo."),
        })

    def action_acknowledge_without_booking(self):
        """Take a document off the OpenMES backlog without booking it in Odoo
        (for example after booking it by hand); releases the stock snapshot.

        The decision is stored (and committed) before the acknowledgement is
        sent: the document becomes booked with “Not booked” handling, so a
        failed acknowledgement is only re-sent later, never booked."""
        self._openmes_check_manager()
        records = self.sudo().filtered(lambda r: r.state in UNBOOKED_STATES)
        records.write({
            "state": "booked",
            "handling": "manual",
            "erp_reference": NOT_BOOKED_REFERENCE,
            "error_message": False,
            "attempt_count": 0,
        })
        self._openmes_commit()
        records._openmes_acknowledge()
        failed = records.filtered(lambda r: r.state != "acked")
        if failed:
            return self._notify(
                _("%(count)s stock document(s) could not be acknowledged. See the message on each document.",
                  count=len(failed)),
                "warning",
            )
        return self._notify(_("Stock documents acknowledged without booking."), "success")

    def _notify(self, message, kind):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "message": message,
                "type": kind,
                "sticky": kind != "success",
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    # ── Fetching ─────────────────────────────────────────────────────────────

    @api.model
    def _openmes_find_warehouse(self, codes, company=None):
        """Warehouses of ``company`` (default: the current one) with one of ``codes``."""
        codes = [c for c in codes if c]
        if not codes:
            return self.env["stock.warehouse"]
        company = company or self.env.company
        return self.env["stock.warehouse"].sudo().search(
            [("openmes_code", "in", codes), ("company_id", "=", company.id)]
        )

    @staticmethod
    def _openmes_float(value):
        """``value`` as a float, ``None`` when it is not a number."""
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @api.model
    def _openmes_header_vals(self, row):
        mes_type = row.get("type") or False
        warehouses = self._openmes_find_warehouse([row.get("warehouse_code"), row.get("warehouse_erp_code")])
        work_order = self.env["openmes.work.order"]
        if row.get("work_order_no"):
            work_order = work_order.sudo().search(
                [("name", "=", row["work_order_no"]), ("company_id", "=", self.env.company.id)], limit=1
            )
        lines = [line for line in (row.get("lines") or []) if isinstance(line, dict)]
        return {
            "name": (row.get("document_no") or str(row["id"]))[:255],
            "document_type": mes_type if mes_type in dict(DOCUMENT_TYPES) else False,
            "mes_type": mes_type,
            "direction": row.get("direction") if row.get("direction") in ("in", "out") else False,
            "posted_at": parse_iso_datetime(row.get("posted_at")),
            "warehouse_code": row.get("warehouse_code") or False,
            "warehouse_erp_code": row.get("warehouse_erp_code") or False,
            "warehouse_id": warehouses.id if len(warehouses) == 1 else False,
            "work_order_no": row.get("work_order_no") or False,
            "work_order_id": work_order.id,
            "production_id": work_order.production_id.id,
            "line_ids": [(5, 0, 0)] + [
                (0, 0, {
                    "material_code": line.get("material_code") or False,
                    "product_type_code": line.get("product_type_code") or False,
                    "lot_number": line.get("lot_number") or False,
                    "quantity": self._openmes_float(line.get("quantity")) or 0.0,
                    "invalid_quantity": (
                        str(line.get("quantity"))[:64]
                        if self._openmes_float(line.get("quantity")) is None
                        else False
                    ),
                    "unit_of_measure": line.get("unit_of_measure") or False,
                    "notes": line.get("notes") or False,
                })
                for line in lines
            ],
        }

    @api.model
    def _openmes_fetch(self):
        """Store the OpenMES backlog. Returns ``(documents on the backlog,
        error messages)``; raises OpenmesApiError when it cannot be read."""
        client = self.env["openmes.client"].sudo().with_company(self.env.company)
        # Every page first: acknowledging while paging would shift the
        # unsynced_only cursor and skip documents.
        rows = list(client._iter_export("/stock-documents", {"unsynced_only": 1}))
        Document = self.sudo().browse()
        errors = []
        fetched = Document
        for row in rows:
            try:
                mes_id = int(row["id"])
                if mes_id <= 0:
                    raise ValueError(mes_id)
            except (TypeError, ValueError, KeyError):
                label = row.get("document_no") if isinstance(row, dict) else None
                errors.append(_("A stock document without a valid id was skipped (%s).", label or "?"))
                continue
            try:
                with self.env.cr.savepoint():
                    fetched |= self._openmes_store_row(mes_id, row)
            except Exception:
                # One malformed row must not stop the backlog (and every
                # stock snapshot) for good.
                _logger.exception("Storing OpenMES stock document %s failed", mes_id)
                self.env.invalidate_all()
                errors.append(_("Stock document %s could not be read; see the server log.", mes_id))
        return fetched, errors

    @api.model
    def _openmes_store_row(self, mes_id, row):
        Document = self.sudo()
        company = self.env.company
        document = Document.search([("mes_id", "=", mes_id), ("company_id", "=", company.id)])
        if document and document.state == "booked":
            pass  # booked here already; only the acknowledgement is missing
        elif document and document.state == "acked" and document.handling != "external":
            # OpenMES lost our acknowledgement: send it again, never rebook.
            document.write({"state": "booked", "attempt_count": 0})
        elif document:
            document.write(dict(
                self._openmes_header_vals(row), state="pending", handling=False, error_message=False
            ))
        else:
            document = Document.create(dict(self._openmes_header_vals(row), mes_id=mes_id, company_id=company.id))
        return document

    # ── Booking ──────────────────────────────────────────────────────────────

    def _openmes_book(self):
        """Book every pending document of ``self``. Never raises: a problem is
        stored on the document and the others carry on."""
        for record in self.filtered(lambda r: r.state in UNBOOKED_STATES):
            error = None
            try:
                with self.env.cr.savepoint():
                    record._openmes_book_one()
            except (UserError, RedirectWarning) as exc:
                error = str(exc)
            except Exception:
                _logger.exception("Booking OpenMES stock document %s failed", record.mes_id)
                error = _("Unexpected error; see the server log.")
            if error:
                self.env.invalidate_all()
                record.write({"state": "error", "error_message": error})

    def _openmes_book_one(self):
        self.ensure_one()
        work_order = self.work_order_id
        if not work_order and self.work_order_no:
            work_order = self.env["openmes.work.order"].sudo().search(
                [("name", "=", self.work_order_no), ("company_id", "=", self.company_id.id)], limit=1
            )
        production = work_order.production_id
        # By source, not by the current mode: an MO consumes its components and
        # receives its product when it is marked as done, so booking the
        # OpenMES document as well would count it twice.
        if production and production.state != "cancel":
            vals = {"work_order_id": work_order.id, "production_id": production.id}
            if production.state == "done":
                vals.update(
                    state="booked",
                    handling="production",
                    erp_reference=production.name,
                    error_message=False,
                )
            else:
                vals.update(
                    state="waiting_production",
                    error_message=_(
                        "Waiting for manufacturing order %s to be marked as done; it books this "
                        "consumption itself.",
                        production.name,
                    ),
                )
            self.write(vals)
            return

        if not self.document_type:
            raise UserError(_("Unknown OpenMES document type “%s”.", self.mes_type or ""))
        warehouse = self._openmes_find_warehouse([self.warehouse_code, self.warehouse_erp_code], self.company_id)
        codes = " / ".join(c for c in (self.warehouse_code, self.warehouse_erp_code) if c)
        if not warehouse:
            raise UserError(
                _("Unknown OpenMES warehouse “%s”: set it as the OpenMES Warehouse Code of an Odoo warehouse.", codes)
            )
        if len(warehouse) > 1:
            raise UserError(_("Several Odoo warehouses have the OpenMES code “%s”.", codes))
        if not self.line_ids:
            raise UserError(_("The document has no lines."))

        company = warehouse.company_id
        stock_location = warehouse.lot_stock_id
        move_vals = []
        for line in self.line_ids:
            product, lot, qty = line._openmes_resolve(company)
            production_location = self._openmes_production_location(product, company)
            if self.document_type in OUTGOING_TYPES:
                source, destination = stock_location, production_location
            else:
                source, destination = production_location, stock_location
            line.product_id = product
            move_vals.append({
                "origin": self.name,
                "product_id": product.id,
                "uom_id": product.uom_id.id,
                "product_uom_qty": qty,
                "location_id": source.id,
                "location_dest_id": destination.id,
                "company_id": company.id,
                "openmes_stock_document_id": self.id,
                "picked": True,
                "move_line_ids": [(0, 0, {
                    "product_id": product.id,
                    "uom_id": product.uom_id.id,
                    "quantity": qty,
                    "location_id": source.id,
                    "location_dest_id": destination.id,
                    "lot_id": lot.id,
                    "company_id": company.id,
                })],
            })
        moves = self.env["stock.move"].sudo().with_company(company).create(move_vals)
        moves._action_done()
        not_done = moves.filtered(lambda m: m.state != "done")
        if not_done:
            raise UserError(_("Odoo could not complete the stock moves."))
        self.write({
            "warehouse_id": warehouse.id,
            "state": "booked",
            "handling": "moves",
            "erp_reference": moves[0].reference,
            "error_message": False,
        })

    @api.model
    def _openmes_production_location(self, product, company):
        location = product.with_company(company).property_stock_production
        if location and location.company_id in (company, self.env["res.company"]):
            return location
        location = self.env["stock.location"].sudo().search(
            [("usage", "=", "production"), ("company_id", "in", [company.id, False])], limit=1
        )
        if not location:
            raise UserError(_("Company %s has no production location.", company.name))
        return location

    # ── Acknowledging ────────────────────────────────────────────────────────

    def _openmes_acknowledge(self):
        """Send the acknowledgement; on failure keep the document booked so the
        next run only re-sends it."""
        for record in self:
            client = self.env["openmes.client"].sudo().with_company(record.company_id)
            try:
                client._ack_stock_document(record.mes_id, record.erp_reference)
            except OpenmesApiError as exc:
                if exc.status_code == 404:
                    # Permanent: OpenMES does not know the id (or it is another
                    # tenant's). No automatic retry; Retry still re-sends it.
                    record.write({
                        "ack_error": _(
                            "OpenMES does not know this document (HTTP 404); it is not acknowledged "
                            "automatically any more. Check the document in OpenMES."
                        ),
                        "attempt_count": MAX_AUTO_ATTEMPTS,
                    })
                else:
                    record.write({"ack_error": str(exc), "attempt_count": record.attempt_count + 1})
                continue
            record.write({"state": "acked", "acked_at": fields.Datetime.now(), "ack_error": False, "attempt_count": 0})

    # ── Entry points ─────────────────────────────────────────────────────────

    @api.model
    def _openmes_commit(self):
        """Commit the bookings before any acknowledgement is sent.

        An acknowledgement takes the document off the OpenMES backlog for
        good, so it must never be sent for a booking that could still be
        rolled back (a serialization failure replays the request, and the
        replay would no longer see the document). Every run commits here —
        crons and buttons alike; after a commit, a replay only re-sends the
        acknowledgements of documents that are already booked. Tests run in
        one transaction, so there it is a no-op.
        """
        if not modules.module.current_test:
            self.env.cr.commit()

    @api.model
    def _openmes_rollback(self):
        """Counterpart of ``_openmes_commit`` after a failed run (no-op in tests)."""
        if not modules.module.current_test:
            self.env.cr.rollback()

    @api.model
    def _openmes_try_lock(self):
        """Transaction-level advisory lock: one run books at a time.

        Released by the commit that precedes the acknowledgements, so two
        runs may acknowledge at the same time; that is harmless (an ack is
        idempotent in OpenMES and the document row write serialises them)."""
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (BOOKING_LOCK_KEY,))
        return self.env.cr.fetchone()[0]

    @api.model
    def _openmes_failed_log(self, message):
        return self.env["openmes.sync.log"].sudo().create({
            "company_id": self.env.company.id,
            "operation": "stock_documents",
            "state": "failed",
            "error_count": 1,
            "message": message,
        })

    @api.model
    def _openmes_sync_documents(self):
        """Fetch, book and acknowledge the documents of ``self.env.company``.
        Returns ``(sync log, backlog_read)``; ``backlog_read`` is False when the
        backlog could not be read or booked (OpenMES unreachable, or another
        run is booking right now)."""
        company = self.env.company
        Document = self.sudo().with_company(company)
        if not self._openmes_try_lock():
            return self._openmes_failed_log(
                _("Another run is booking the OpenMES stock documents right now; skipped.")
            ), False
        try:
            fetched, fetch_errors = Document._openmes_fetch()
        except OpenmesApiError as exc:
            return self._openmes_failed_log(_("Stock documents could not be read from OpenMES: %s", exc)), False

        # Only what is still on the backlog is booked: a document that left it
        # was acknowledged in OpenMES by someone else (e.g. booked by hand),
        # so booking it here would count it twice.
        # Skipped when a row could not be read: it may be one of them.
        gone = Document
        if not fetch_errors:
            gone = Document.search([
                ("company_id", "=", company.id),
                ("state", "in", UNBOOKED_STATES),
                ("id", "not in", fetched.ids),
            ])
        gone._openmes_mark_external()
        to_book = fetched.filtered(lambda d: d.state in UNBOOKED_STATES)
        to_book._openmes_book()
        self._openmes_commit()
        to_ack = Document.search([
            ("company_id", "=", company.id),
            ("state", "=", "booked"),
            ("attempt_count", "<", MAX_AUTO_ATTEMPTS),
        ])
        to_ack._openmes_acknowledge()

        errors = list(fetch_errors) + [
            f"{doc.name}: {doc.error_message}" for doc in fetched if doc.state == "error" and doc.error_message
        ] + [f"{doc.name}: {doc.ack_error}" for doc in to_ack if doc.state == "booked" and doc.ack_error]
        message = "\n".join(errors[:MAX_LOGGED_ERRORS])
        if len(errors) > MAX_LOGGED_ERRORS:
            message += "\n" + _("… and %s more.", len(errors) - MAX_LOGGED_ERRORS)
        log = self.env["openmes.sync.log"].sudo().create({
            "company_id": company.id,
            "operation": "stock_documents",
            "state": "partial" if errors else "success",
            "sent_count": len(fetched),
            "imported_count": len(to_book.filtered(lambda d: d.state in ("booked", "acked"))),
            "updated_count": len(to_ack.filtered(lambda d: d.state == "acked")),
            "skipped_count": len(gone),
            "error_count": len(errors),
            "message": message or False,
        })
        return log, True

    @api.model
    def _openmes_blocking(self, warehouse):
        """Documents of ``warehouse`` that are not in Odoo's stock yet."""
        code = warehouse.openmes_code
        domain = [("warehouse_id", "=", warehouse.id)]
        if code:
            domain = ["|", "|"] + domain + [("warehouse_code", "=", code), ("warehouse_erp_code", "=", code)]
        return self.sudo().search(
            [("state", "in", UNBOOKED_STATES), ("company_id", "=", warehouse.company_id.id)] + domain
        )

    @api.model
    def _cron_openmes_stock_documents(self):
        # The booking lock is released by the commit of each run, so every
        # company takes it again.
        self.env["res.company"]._openmes_cron_each(
            "stock document run",
            lambda company: self.sudo().with_company(company)._openmes_sync_documents(),
            commit=True,
        )


class OpenmesStockDocumentLine(models.Model):
    _name = "openmes.stock.document.line"
    _description = "OpenMES Stock Document Line"
    _order = "id"

    document_id = fields.Many2one("openmes.stock.document", required=True, ondelete="cascade", index=True)
    material_code = fields.Char(readonly=True)
    product_type_code = fields.Char(readonly=True)
    product_id = fields.Many2one("product.product", "Product", readonly=True)
    lot_number = fields.Char("Lot Number", readonly=True)
    quantity = fields.Float(digits="Product Unit", readonly=True)
    invalid_quantity = fields.Char(
        readonly=True, help="The quantity exactly as OpenMES sent it, when it was not a number."
    )
    unit_of_measure = fields.Char("Unit", readonly=True)
    notes = fields.Char(readonly=True)

    def _openmes_resolve(self, company):
        """Return ``(product, lot, quantity in the product's unit)`` or raise UserError."""
        self.ensure_one()
        code = self.material_code or self.product_type_code
        if not code:
            raise UserError(_("A line has no material or product code."))
        products = self.env["product.product"].sudo().search(
            [("default_code", "=", code), ("company_id", "in", [False, company.id])]
        )
        if not products:
            raise UserError(_("Unknown product “%s”: no product has this Internal Reference.", code))
        if len(products) > 1:
            raise UserError(_("Several products have the Internal Reference “%s”.", code))
        product = products
        if not product.is_storable:
            raise UserError(_("Product “%s” does not track inventory.", code))

        if self.invalid_quantity:
            raise UserError(_("Quantity “%(qty)s” of “%(product)s” is not a number.",
                              qty=self.invalid_quantity, product=code))
        uom = product.uom_id
        if not self.unit_of_measure:
            raise UserError(_("The line of “%s” has no unit of measure.", code))
        names = {uom.name, uom.with_context(lang="en_US").name}
        if not units_match(self.unit_of_measure, names):
            raise UserError(
                _(
                    "Unit “%(unit)s” does not match the unit of “%(product)s” (%(uom)s); units are not converted.",
                    unit=self.unit_of_measure,
                    product=code,
                    uom=uom.name,
                )
            )
        qty = uom.round(self.quantity)
        if uom.compare(qty, 0.0) <= 0:
            raise UserError(_("Quantity of “%s” must be greater than zero.", code))

        lot = self.env["stock.lot"]
        if product.tracking in ("lot", "serial"):
            if not self.lot_number:
                raise UserError(_("Product “%s” is tracked by lot or serial number, but the line has none.", code))
            if product.tracking == "serial" and uom.compare(qty, 1.0):
                raise UserError(_("Serial-tracked product “%s” must come with quantity 1 per line.", code))
            Lot = self.env["stock.lot"].sudo().with_company(company)
            lot = Lot.search(
                [("name", "=", self.lot_number), ("product_id", "=", product.id),
                 ("company_id", "in", [company.id, False])],
                limit=1,
            ) or Lot.create({"name": self.lot_number, "product_id": product.id, "company_id": company.id})
        return product, lot, qty
