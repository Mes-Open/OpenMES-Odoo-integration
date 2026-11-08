from unittest.mock import patch

import psycopg2
import requests

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from .common import FakeResponse, OpenmesCase, import_report


@tagged("post_install", "-at_install")
class TestStockDocuments(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.warehouse.openmes_code = "0100"
        cls.stock = cls.warehouse.lot_stock_id
        cls.production_location = cls.flour.property_stock_production
        cls.Document = cls.env["openmes.stock.document"]
        # Flour only: an MO of buns closes without lots.
        cls.bun = cls.env["product.product"].create({
            "name": "Bun", "default_code": "BUN-01", "is_storable": True, "openmes_manufacture": True,
        })
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.bun.product_tmpl_id.id,
            "product_qty": 10,
            "bom_line_ids": [(0, 0, {"product_id": cls.flour.id, "product_qty": 1})],
        })

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _line(code, qty, unit, lot=None, product=False):
        return {
            "material_code": None if product else code,
            "product_type_code": code if product else None,
            "lot_number": lot,
            "quantity": qty,
            "unit_of_measure": unit,
            "notes": None,
        }

    @staticmethod
    def _doc(mes_id, doc_type, lines, wh="RAW-1", erp="0100", work_order_no=None, number=None):
        return {
            "id": mes_id,
            "document_no": number or f"DOC/2026/{mes_id:04d}",
            "type": doc_type,
            "status": "posted",
            "direction": "out" if doc_type in ("material_issue", "product_issue") else "in",
            "warehouse_code": wh,
            "warehouse_erp_code": erp,
            "work_order_no": work_order_no,
            "erp_reference": None,
            "erp_synced_at": None,
            "posted_at": "2026-11-03T18:37:13+00:00",
            "lines": lines,
        }

    @staticmethod
    def _page(docs, next_cursor=None):
        return FakeResponse(200, {"data": docs, "meta": {"next_cursor": next_cursor, "has_more": bool(next_cursor)}})

    @staticmethod
    def _ack(status=200):
        return FakeResponse(status, {"data": {"id": 1}} if status < 300 else {"message": "error"})

    def _run(self):
        return self.Document._openmes_sync_documents()

    def _on_hand(self, product, lot=None, location=None):
        domain = [("product_id", "=", product.id), ("location_id", "child_of", (location or self.stock).id)]
        if lot is not None:
            domain.append(("lot_id", "=", lot.id))
        return sum(self.env["stock.quant"].search(domain).mapped("quantity"))

    def _document(self, mes_id):
        return self.Document.search([("mes_id", "=", mes_id)])

    @staticmethod
    def _path(call):
        return call["url"].rsplit("/api/v1/erp", 1)[1]

    def _mo_work_order(self, name="ODOO-WO-MO-1", qty=10):
        production = self.env["mrp.production"].create({
            "product_id": self.bun.id, "product_qty": qty, "product_uom_id": self.bun.uom_id.id,
        })
        production.action_confirm()
        work_order = self.env["openmes.work.order"].create({
            "name": name, "source_type": "production", "production_id": production.id,
            "product_id": self.bun.id, "planned_qty": qty, "state": "sent",
        })
        return production, work_order

    def _mark_done(self, production):
        production.qty_producing = production.product_qty
        production._set_qty_producing()
        production.with_context(skip_backorder=True).button_mark_done()
        self.assertEqual(production.state, "done")

    # ── Happy paths ──────────────────────────────────────────────────────────

    def test_material_issue_with_new_lot(self):
        self.env["stock.quant"]._update_available_quantity(self.flour, self.stock, 10)
        doc = self._doc(3, "material_issue", [
            self._line("YEAST-01", 2, "kg", lot="L-1"),
            self._line("FLOUR-01", 4, "kg"),
        ], number="MI/2026/0001")
        with self.mock_openmes(self._page([doc]), self._ack()) as calls:
            log, read = self._run()

        self.assertTrue(read)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(self._path(calls[0]), "/stock-documents")
        self.assertEqual(calls[0]["params"]["unsynced_only"], 1)
        self.assertEqual(calls[1]["method"], "POST")
        self.assertEqual(self._path(calls[1]), "/stock-documents/3/ack")
        self.assertEqual(calls[1]["json"], {"erp_reference": "OpenMES MI/2026/0001"})

        document = self._document(3)
        self.assertEqual(document.state, "acked")
        self.assertEqual(document.handling, "moves")
        self.assertEqual(document.erp_reference, "OpenMES MI/2026/0001")
        self.assertEqual(document.warehouse_id, self.warehouse)
        self.assertEqual(len(document.move_ids), 2)
        self.assertEqual(set(document.move_ids.mapped("state")), {"done"})
        self.assertEqual(document.move_ids.location_id, self.stock)
        self.assertEqual(document.move_ids.location_dest_id.usage, "production")
        self.assertEqual(set(document.move_ids.mapped("reference")), {"OpenMES MI/2026/0001"})
        lot = self.env["stock.lot"].search([("name", "=", "L-1"), ("product_id", "=", self.yeast.id)])
        self.assertTrue(lot, "the lot is created for a tracked product")
        self.assertEqual(document.move_ids.move_line_ids.filtered(lambda l: l.product_id == self.yeast).lot_id, lot)
        self.assertEqual(self._on_hand(self.flour), 6)
        self.assertEqual(self._on_hand(self.yeast, lot), -2, "MES consumed it physically; quants may go negative")
        self.assertEqual((log.operation, log.state, log.sent_count, log.imported_count, log.updated_count),
                         ("stock_documents", "success", 1, 1, 1))

    def test_product_receipt_reuses_existing_lot(self):
        self.bread.tracking = "lot"
        lot = self.env["stock.lot"].create({"name": "B-7", "product_id": self.bread.id, "company_id": self.env.company.id})
        doc = self._doc(4, "product_receipt", [self._line("BREAD-01", 12, "pcs", lot="B-7", product=True)])
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        document = self._document(4)
        self.assertEqual(document.state, "acked")
        self.assertEqual(document.move_ids.location_id.usage, "production")
        self.assertEqual(document.move_ids.location_dest_id, self.stock)
        self.assertEqual(self._on_hand(self.bread, lot), 12)
        self.assertEqual(self.env["stock.lot"].search_count([("name", "=", "B-7")]), 1)

    def test_reverse_types(self):
        self.env["stock.quant"]._update_available_quantity(self.bread, self.stock, 5)
        docs = [
            self._doc(5, "material_receipt", [self._line("FLOUR-01", 3, "kg")]),
            self._doc(6, "product_issue", [self._line("BREAD-01", 2, "Units", product=True)]),
        ]
        with self.mock_openmes(self._page(docs), self._ack(), self._ack()):
            self._run()
        returned, issued = self._document(5), self._document(6)
        self.assertEqual(returned.move_ids.location_dest_id, self.stock)
        self.assertEqual(returned.move_ids.location_id.usage, "production")
        self.assertEqual(issued.move_ids.location_id, self.stock)
        self.assertEqual(issued.move_ids.location_dest_id.usage, "production")
        self.assertEqual(self._on_hand(self.flour), 3)
        self.assertEqual(self._on_hand(self.bread), 3)

    def test_warehouse_matched_by_code(self):
        self.warehouse.openmes_code = "RAW-1"
        doc = self._doc(7, "material_receipt", [self._line("FLOUR-01", 1, "kg")], wh="RAW-1", erp=None)
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        self.assertEqual(self._document(7).warehouse_id, self.warehouse)
        self.assertEqual(self._document(7).state, "acked")

    def test_unit_alias_is_accepted(self):
        doc = self._doc(8, "product_receipt", [self._line("BREAD-01", 4, "szt.", product=True)])
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        self.assertEqual(self._document(8).state, "acked")
        self.assertEqual(self._on_hand(self.bread), 4)

    def test_cursor_pages_are_read_before_acknowledging(self):
        first = self._doc(9, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        second = self._doc(10, "material_receipt", [self._line("FLOUR-01", 2, "kg")])
        with self.mock_openmes(self._page([first], "c2"), self._page([second]), self._ack(), self._ack()) as calls:
            self._run()
        self.assertEqual([c["method"] for c in calls], ["GET", "GET", "POST", "POST"])
        self.assertEqual(calls[1]["params"]["cursor"], "c2")
        self.assertEqual(self._on_hand(self.flour), 3)

    def test_empty_ack_answer_is_success(self):
        doc = self._doc(11, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        with self.mock_openmes(self._page([doc]), FakeResponse(204)):
            self._run()
        self.assertEqual(self._document(11).state, "acked")

    # ── Idempotency and OpenMES errors ───────────────────────────────────────

    def test_second_run_does_not_book_again(self):
        doc = self._doc(12, "material_receipt", [self._line("FLOUR-01", 5, "kg")])
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        # OpenMES lost the acknowledgement and returns the document again.
        with self.mock_openmes(self._page([doc]), self._ack()) as calls:
            self._run()
        self.assertEqual([self._path(c) for c in calls], ["/stock-documents", "/stock-documents/12/ack"])
        self.assertEqual(len(self._document(12).move_ids), 1)
        self.assertEqual(self._on_hand(self.flour), 5)
        # Nothing new: only the GET.
        with self.mock_openmes(self._page([])) as calls:
            self._run()
        self.assertEqual(len(calls), 1)

    def test_ack_failure_is_retried_without_rebooking(self):
        doc = self._doc(13, "material_receipt", [self._line("FLOUR-01", 5, "kg")])
        with self.mock_openmes(self._page([doc]), self._ack(500)):
            log, _read = self._run()
        document = self._document(13)
        self.assertEqual(document.state, "booked")
        self.assertTrue(document.ack_error)
        self.assertEqual(document.attempt_count, 1)
        self.assertEqual(log.state, "partial")

        # Still on the backlog: returned again, only acknowledged.
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        self.assertEqual(document.state, "acked")
        self.assertFalse(document.ack_error)
        self.assertEqual(len(document.move_ids), 1)
        self.assertEqual(self._on_hand(self.flour), 5)

    def test_ack_timeout_and_retry_limit(self):
        doc = self._doc(14, "material_receipt", [self._line("FLOUR-01", 1, "kg")])

        def timeout(method, url, **kwargs):
            raise requests.Timeout("slow")

        with self.mock_openmes(self._page([doc]), timeout):
            self._run()
        document = self._document(14)
        self.assertEqual(document.state, "booked")
        for _attempt in range(4):
            with self.mock_openmes(self._page([]), self._ack(500)):
                self._run()
        self.assertEqual(document.attempt_count, 5)
        with self.mock_openmes(self._page([])) as calls:
            self._run()
        self.assertEqual(len(calls), 1, "no automatic ack after five failures")
        # A manual retry still sends it.
        with self.mock_openmes(self._ack()):
            document.action_retry()
        self.assertEqual(document.state, "acked")
        self.assertEqual(len(document.move_ids), 1)

    def test_ack_404_is_not_retried_automatically(self):
        doc = self._doc(35, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        with self.mock_openmes(self._page([doc]), self._ack(404)):
            self._run()
        document = self._document(35)
        self.assertEqual(document.state, "booked")
        self.assertIn("404", document.ack_error)
        with self.mock_openmes(self._page([])) as calls:
            self._run()
        self.assertEqual(len(calls), 1, "a permanent error is not re-sent by the cron")

    def test_non_json_backlog_answer_is_not_an_empty_backlog(self):
        doc = self._doc(36, "material_issue", [self._line("NOPE-01", 1, "kg")])
        with self.mock_openmes(self._page([doc])):
            self._run()
        # A proxy or maintenance page answering 200 with HTML.
        with self.mock_openmes(FakeResponse(200, None)) as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual(len(calls), 1, "no snapshot")
        self.assertEqual(self._document(36).state, "error", "not given up as acknowledged elsewhere")
        self.assertEqual(logs.mapped("state"), ["failed", "failed"])

    def test_backlog_read_failures(self):
        for status in (401, 403, 500):
            with self.mock_openmes(FakeResponse(status, {"message": "no"})):
                log, read = self._run()
            self.assertFalse(read)
            self.assertEqual((log.operation, log.state), ("stock_documents", "failed"))
        self.assertFalse(self.Document.search([]))

    def test_cron_runs_only_when_enabled(self):
        self._configure(enabled=False)
        with self.mock_openmes():
            self.Document._cron_openmes_stock_documents()
        self._configure(enabled=True)
        with self.mock_openmes(self._page([])) as calls:
            self.Document._cron_openmes_stock_documents()
        self.assertEqual(len(calls), 1)

    # ── Crash safety and overlapping runs ────────────────────────────────────

    def _record_commits(self, calls):
        def commit(_model):
            calls.append({"method": "COMMIT", "url": "commit"})
        return patch.object(type(self.Document), "_openmes_commit", autospec=True, side_effect=commit)

    def test_bookings_are_committed_before_any_ack(self):
        doc = self._doc(15, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        with self.mock_openmes(self._page([doc]), self._ack(), import_report(updated=4)) as calls, \
                self._record_commits(calls):
            self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual([c["method"] for c in calls], ["GET", "COMMIT", "POST", "POST"])

        # The settings button and Retry too, not only the cron.
        doc = self._doc(16, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        with self.mock_openmes(self._page([doc]), self._ack()) as calls, self._record_commits(calls):
            self.env["res.config.settings"].create({}).action_openmes_sync_stock_documents()
        self.assertEqual([c["method"] for c in calls], ["GET", "COMMIT", "POST"])
        document = self._error_document()
        self._create_nope()
        with self.mock_openmes(self._page([self._error_row()]), self._ack()) as calls, self._record_commits(calls):
            document.action_retry()
        self.assertEqual([c["method"] for c in calls], ["GET", "COMMIT", "POST"])
        # Acknowledge Without Booking stores its decision before the ack.
        doc = self._doc(17, "material_issue", [self._line("SALT-01", 1, "kg")])
        with self.mock_openmes(self._page([doc])):
            self._run()
        with self.mock_openmes(self._ack()) as calls, self._record_commits(calls):
            self._document(17).action_acknowledge_without_booking()
        self.assertEqual([c["method"] for c in calls], ["COMMIT", "POST"])

    def test_overlapping_run_is_skipped_with_its_snapshot(self):
        with patch.object(type(self.Document), "_openmes_try_lock", autospec=True, return_value=False), \
                self.mock_openmes() as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()
        self.assertFalse(calls)
        self.assertEqual(logs.mapped("state"), ["failed", "failed"])
        self.assertIn("Another run", logs.filtered(lambda l: l.operation == "stock").message)

    def test_malformed_rows_do_not_stop_the_backlog(self):
        bad_qty = self._doc(17, "material_receipt", [self._line("FLOUR-01", "lots", "kg")])
        good = self._doc(18, "material_receipt", [self._line("FLOUR-01", 2, "kg")])
        no_id = dict(self._doc(19, "material_receipt", []), id="abc")
        with self.mock_openmes(self._page([bad_qty, no_id, "junk", good]), self._ack()):
            log, read = self._run()
        self.assertTrue(read)
        self.assertEqual(self._document(17).state, "error")
        self.assertIn("lots", self._document(17).error_message)
        self.assertEqual(self._document(18).state, "acked")
        self.assertEqual(log.state, "partial")
        self.assertIn("valid id", log.message)

    def test_document_gone_from_backlog_is_not_booked(self):
        doc = self._doc(29, "material_issue", [self._line("NOPE-01", 1, "kg")])
        with self.mock_openmes(self._page([doc])):
            self._run()
        document = self._document(29)
        self.assertEqual(document.state, "error")
        self.env["product.product"].create({"name": "Nope", "default_code": "NOPE-01", "is_storable": True,
                                            "uom_id": self.uom_kg.id})
        # A malformed row: the backlog is incomplete, so nothing is given up.
        with self.mock_openmes(self._page(["junk"])):
            self._run()
        self.assertEqual(document.state, "error")
        # Acknowledged in OpenMES by someone else: never booked here.
        with self.mock_openmes(self._page([])) as calls:
            log, _read = self._run()
        self.assertEqual(len(calls), 1, "no ack for it")
        self.assertEqual((document.state, document.handling), ("acked", "external"))
        self.assertFalse(document.move_ids)
        self.assertEqual(log.skipped_count, 1)

    # ── Booking errors ───────────────────────────────────────────────────────

    def test_unknown_product_does_not_stop_the_others(self):
        bad = self._doc(20, "material_issue", [self._line("NOPE-01", 1, "kg")])
        good = self._doc(21, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        with self.mock_openmes(self._page([bad, good]), self._ack()) as calls:
            log, _read = self._run()
        self.assertEqual([self._path(c) for c in calls], ["/stock-documents", "/stock-documents/21/ack"])
        document = self._document(20)
        self.assertEqual(document.state, "error")
        self.assertIn("NOPE-01", document.error_message)
        self.assertFalse(document.move_ids)
        self.assertEqual(self._document(21).state, "acked")
        self.assertEqual(log.state, "partial")
        self.assertIn("NOPE-01", log.message)

    def test_unknown_warehouse(self):
        doc = self._doc(22, "material_receipt", [self._line("FLOUR-01", 1, "kg")], wh="X-9", erp="X-9")
        with self.mock_openmes(self._page([doc])):
            self._run()
        document = self._document(22)
        self.assertEqual(document.state, "error")
        self.assertIn("X-9", document.error_message)
        self.assertEqual(document.company_id, self.env.company, "the company whose OpenMES sent it")

    def test_unit_mismatch(self):
        doc = self._doc(23, "material_issue", [self._line("FLOUR-01", 500, "g")])
        with self.mock_openmes(self._page([doc])):
            self._run()
        document = self._document(23)
        self.assertEqual(document.state, "error")
        self.assertIn("FLOUR-01", document.error_message)
        self.assertFalse(self.env["stock.move"].search([("openmes_stock_document_id", "=", document.id)]))

    def test_line_without_unit(self):
        doc = self._doc(34, "material_receipt", [self._line("FLOUR-01", 1, None)])
        with self.mock_openmes(self._page([doc])):
            self._run()
        self.assertEqual(self._document(34).state, "error")
        self.assertIn("unit", self._document(34).error_message)

    def test_tracked_product_without_lot(self):
        doc = self._doc(24, "material_issue", [self._line("YEAST-01", 1, "kg")])
        with self.mock_openmes(self._page([doc])):
            self._run()
        self.assertEqual(self._document(24).state, "error")
        self.assertIn("YEAST-01", self._document(24).error_message)

    def test_unknown_type_and_zero_quantity(self):
        docs = [
            self._doc(25, "transfer", [self._line("FLOUR-01", 1, "kg")]),
            self._doc(26, "material_receipt", [self._line("FLOUR-01", 0, "kg")]),
        ]
        with self.mock_openmes(self._page(docs)):
            self._run()
        self.assertEqual(self._document(25).state, "error")
        self.assertIn("transfer", self._document(25).error_message)
        self.assertEqual(self._document(26).state, "error")

    def test_one_bad_line_books_nothing(self):
        doc = self._doc(27, "material_receipt", [
            self._line("FLOUR-01", 3, "kg"),
            self._line("NOPE-01", 1, "kg"),
        ])
        with self.mock_openmes(self._page([doc])):
            self._run()
        self.assertEqual(self._document(27).state, "error")
        self.assertFalse(self.env["stock.move"].search([("origin", "=", "DOC/2026/0027")]))
        self.assertEqual(self._on_hand(self.flour), 0)

    def test_error_document_is_booked_once_fixed(self):
        doc = self._doc(28, "material_receipt", [self._line("SUGAR-01", 2, "kg")])
        with self.mock_openmes(self._page([doc])):
            self._run()
        self.assertEqual(self._document(28).state, "error")
        sugar = self.env["product.product"].create({
            "name": "Sugar", "default_code": "SUGAR-01", "is_storable": True,
            "uom_id": self.uom_kg.id,
        })
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        document = self._document(28)
        self.assertEqual(document.state, "acked")
        self.assertFalse(document.error_message)
        self.assertEqual(self._on_hand(sugar), 2)

    # ── Manufacturing orders ─────────────────────────────────────────────────

    def test_done_mo_is_only_acknowledged(self):
        production, work_order = self._mo_work_order()
        self._mark_done(production)
        flour_after_mo = self._on_hand(self.flour)
        doc = self._doc(30, "material_issue", [self._line("FLOUR-01", 1, "kg")], work_order_no=work_order.name)
        with self.mock_openmes(self._page([doc]), self._ack()) as calls:
            self._run()
        document = self._document(30)
        self.assertEqual(document.state, "acked")
        self.assertEqual(document.handling, "production")
        self.assertEqual(document.production_id, production)
        self.assertFalse(document.move_ids)
        self.assertEqual(calls[1]["json"], {"erp_reference": production.name})
        self.assertEqual(self._on_hand(self.flour), flour_after_mo, "not consumed twice")

    def test_open_mo_waits_then_acknowledges(self):
        production, work_order = self._mo_work_order()
        doc = self._doc(31, "product_receipt", [self._line("BUN-01", 10, "Units", product=True)],
                        work_order_no=work_order.name)
        with self.mock_openmes(self._page([doc])):
            self._run()
        document = self._document(31)
        self.assertEqual(document.state, "waiting_production")
        self.assertIn(production.name, document.error_message)

        self._mark_done(production)
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        self.assertEqual(document.state, "acked")
        self.assertFalse(document.move_ids)
        self.assertEqual(self._on_hand(self.bun), 10, "received once, by the MO")

    def test_cancelled_mo_document_is_booked(self):
        production, work_order = self._mo_work_order()
        production.action_cancel()
        doc = self._doc(32, "material_issue", [self._line("FLOUR-01", 1, "kg")], work_order_no=work_order.name)
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        document = self._document(32)
        self.assertEqual((document.state, document.handling), ("acked", "moves"))
        self.assertEqual(self._on_hand(self.flour), -1)

    def test_sale_line_work_order_in_mo_mode_is_booked(self):
        self._configure(order_source="manufacturing_orders")
        order = self._create_order([(self.bread, 3, {})])
        work_order = self.env["openmes.work.order"].create({
            "name": "ODOO-SO-1", "source_type": "sale_line", "sale_order_id": order.id,
            "sale_line_id": order.order_line.id, "product_id": self.bread.id, "planned_qty": 3,
        })
        doc = self._doc(33, "product_receipt", [self._line("BREAD-01", 3, "pcs", product=True)],
                        work_order_no=work_order.name)
        with self.mock_openmes(self._page([doc]), self._ack()):
            self._run()
        self.assertEqual(self._document(33).handling, "moves")
        self.assertEqual(self._on_hand(self.bread), 3)

    # ── Stock snapshot gating ────────────────────────────────────────────────

    def test_stock_sync_books_documents_first(self):
        self.env["stock.quant"]._update_available_quantity(self.flour, self.stock, 100)
        doc = self._doc(40, "material_issue", [self._line("FLOUR-01", 30, "kg")])
        with self.mock_openmes(self._page([doc]), self._ack(), import_report(updated=4)) as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual([self._path(c) for c in calls],
                         ["/stock-documents", "/stock-documents/40/ack", "/stock/import"])
        rows = {r.get("material_code"): r for r in calls[2]["json"]["balances"]}
        self.assertEqual(rows["FLOUR-01"]["quantity"], 70)
        self.assertEqual(set(logs.mapped("operation")), {"stock_documents", "stock"})

    def test_unbooked_document_blocks_only_its_warehouse(self):
        other = self.env["stock.warehouse"].create({"name": "Second", "code": "WH2", "openmes_code": "0200"})
        doc = self._doc(41, "material_issue", [self._line("NOPE-01", 1, "kg")])
        with self.mock_openmes(self._page([doc]), import_report(updated=4)) as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()
        imports = [c for c in calls if self._path(c) == "/stock/import"]
        self.assertEqual([c["json"]["warehouse_code"] for c in imports], [other.openmes_code])
        blocked = logs.filtered(lambda l: l.operation == "stock" and l.state == "failed")
        self.assertEqual(len(blocked), 1)
        self.assertIn("DOC/2026/0041", blocked.message)
        self.assertIn("0100", blocked.message)

    def test_waiting_mo_blocks_but_booked_does_not(self):
        production, work_order = self._mo_work_order()
        doc = self._doc(42, "material_issue", [self._line("FLOUR-01", 1, "kg")], work_order_no=work_order.name)
        with self.mock_openmes(self._page([doc])) as calls:
            self.env["openmes.sync"]._sync_all_stock()
        self.assertNotIn("/stock/import", [self._path(c) for c in calls])

        # Booked but the acknowledgement failed: Odoo's stock has it, so no block.
        self._document(42).unlink()
        doc = self._doc(43, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        with self.mock_openmes(self._page([doc]), self._ack(500), import_report(updated=4)) as calls:
            self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual(self._document(43).state, "booked")
        self.assertEqual(self._path(calls[-1]), "/stock/import")

    def test_unreadable_backlog_sends_no_snapshot(self):
        with self.mock_openmes(FakeResponse(403, {"message": "scope"})) as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual(len(calls), 1)
        stock_log = logs.filtered(lambda l: l.operation == "stock")
        self.assertEqual(stock_log.state, "failed")
        self.assertIn("erp:stock:read", stock_log.message)
        self.assertIn("scope", stock_log.message, "the reason is repeated")

    def test_acknowledge_without_booking_releases_the_block(self):
        doc = self._doc(44, "material_issue", [self._line("NOPE-01", 1, "kg")])
        with self.mock_openmes(self._page([doc])):
            self._run()
        document = self._document(44)
        with self.mock_openmes(self._ack()) as calls:
            document.action_acknowledge_without_booking()
        self.assertEqual(calls[0]["json"], {"erp_reference": "Not booked in Odoo"})
        self.assertEqual((document.state, document.handling), ("acked", "manual"))
        self.assertFalse(document.move_ids)
        with self.mock_openmes(self._page([]), import_report(updated=4)) as calls:
            self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual(self._path(calls[-1]), "/stock/import")

    # ── Access rights ────────────────────────────────────────────────────────

    def _error_row(self, qty=1):
        return self._doc(50, "material_issue", [self._line("NOPE-01", qty, "kg")])

    def _error_document(self):
        with self.mock_openmes(self._page([self._error_row()])):
            self._run()
        return self._document(50)

    def _create_nope(self):
        return self.env["product.product"].create({
            "name": "Nope", "default_code": "NOPE-01", "is_storable": True,
            "uom_id": self.uom_kg.id,
        })

    def test_salesman_cannot_read(self):
        document = self._error_document()
        salesman = new_test_user(self.env, "sd_salesman", groups="sales_team.group_sale_salesman")
        with self.assertRaises(AccessError):
            document.with_user(salesman).read(["name"])

    def test_stock_user_reads_but_cannot_act(self):
        document = self._error_document()
        user = new_test_user(self.env, "sd_stock_user", groups="stock.group_stock_user")
        self.assertEqual(document.with_user(user).read(["state"])[0]["state"], "error")
        with self.assertRaises(AccessError):
            document.with_user(user).action_retry()
        with self.assertRaises(AccessError):
            document.with_user(user).action_acknowledge_without_booking()

    def test_managers_cannot_write_documents_directly(self):
        document = self._error_document()
        manager = new_test_user(self.env, "sd_writer", groups="stock.group_stock_manager,mrp.group_mrp_manager")
        with self.assertRaises(AccessError):
            document.with_user(manager).write({"state": "acked"})

    def test_managers_can_retry(self):
        document = self._error_document()
        self.env["product.product"].create({
            "name": "Nope", "default_code": "NOPE-01", "is_storable": True,
            "uom_id": self.uom_kg.id,
        })
        stock_manager = new_test_user(self.env, "sd_stock_mgr", groups="stock.group_stock_manager")
        mrp_manager = new_test_user(self.env, "sd_mrp_mgr", groups="mrp.group_mrp_manager")
        with self.mock_openmes(self._page([self._error_row()]), self._ack(500)):
            document.with_user(stock_manager).action_retry()
        self.assertEqual(document.state, "booked")
        # Booked: only the ack is re-sent, the backlog is not read.
        with self.mock_openmes(self._ack()):
            document.with_user(mrp_manager).action_retry()
        self.assertEqual(document.state, "acked")
        self.assertEqual(len(document.move_ids), 1)

    def test_retry_of_a_still_failing_document_sends_nothing(self):
        document = self._error_document()
        with self.mock_openmes(self._page([self._error_row()])) as calls:
            result = document.action_retry()
        self.assertEqual([c["method"] for c in calls], ["GET"], "only the backlog read, no ack")
        self.assertEqual(document.state, "error")
        self.assertEqual(result["params"]["type"], "warning")

    def test_settings_button_is_admin_only(self):
        user = new_test_user(self.env, "sd_mrp_admin", groups="mrp.group_mrp_manager,stock.group_stock_manager")
        settings = self.env["res.config.settings"].with_user(user).sudo().create({}).with_user(user).sudo(False)
        with self.assertRaises(AccessError):
            settings.action_openmes_sync_stock_documents()
        doc = self._doc(51, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        with self.mock_openmes(self._page([doc]), self._ack()):
            action = self.env["res.config.settings"].create({}).action_openmes_sync_stock_documents()
        self.assertEqual(action["params"]["type"], "success")

    def test_multi_company_rule_and_company_moves(self):
        company_b = self.env["res.company"].create({"name": "Bakery B"})
        self._configure(company_b, enabled=True, api_url="https://mes-b.example.test", api_key="omk_" + "b" * 40)
        warehouse_b = self.env["stock.warehouse"].search([("company_id", "=", company_b.id)], limit=1)
        warehouse_b.openmes_code = "0900"
        doc = self._doc(52, "material_receipt", [self._line("FLOUR-01", 2, "kg")], wh="B-1", erp="0900")
        with self.mock_openmes(self._page([doc]), self._ack()) as calls:
            self.Document.with_company(company_b)._openmes_sync_documents()
        self.assertTrue(all(call["url"].startswith("https://mes-b.example.test/") for call in calls))
        document = self._document(52)
        self.assertEqual(document.company_id, company_b)
        self.assertEqual(document.move_ids.company_id, company_b)
        self.assertEqual(self._on_hand(self.flour, location=warehouse_b.lot_stock_id), 2)

        user = new_test_user(self.env, "sd_stock_a", groups="stock.group_stock_user",
                             company_id=self.env.company.id, company_ids=[(6, 0, [self.env.company.id])])
        self.assertFalse(self.Document.with_user(user).search([("id", "=", document.id)]))

    def test_mes_id_is_unique(self):
        self.Document.create({"name": "A", "mes_id": 60})
        with mute_logger("odoo.sql_db"), self.assertRaises(psycopg2.IntegrityError):
            with self.env.cr.savepoint():
                self.Document.create({"name": "B", "mes_id": 60})

    # ── No double booking: manual ack, retry, delete ─────────────────────────

    def test_acknowledge_without_booking_with_failed_ack_is_never_booked(self):
        document = self._error_document()
        # Booked by hand in Odoo; booking it here would now succeed and count twice.
        nope = self._create_nope()
        with self.mock_openmes(self._ack(500)) as calls:
            result = document.action_acknowledge_without_booking()
        self.assertEqual(calls[0]["json"], {"erp_reference": "Not booked in Odoo"})
        self.assertEqual(result["params"]["type"], "warning")
        self.assertEqual((document.state, document.handling), ("booked", "manual"))
        self.assertEqual(document.erp_reference, "Not booked in Odoo")
        self.assertTrue(document.ack_error)
        self.assertFalse(document.error_message)

        # Still on the backlog: the next run only re-sends the ack, and the
        # snapshot of its warehouse is no longer blocked.
        with self.mock_openmes(self._page([self._error_row()]), self._ack(500), import_report(updated=4)) as calls:
            self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual([self._path(c) for c in calls],
                         ["/stock-documents", "/stock-documents/50/ack", "/stock/import"])
        with self.mock_openmes(self._page([self._error_row()]), self._ack()) as calls:
            self._run()
        self.assertEqual(calls[1]["json"], {"erp_reference": "Not booked in Odoo"})
        self.assertEqual((document.state, document.handling), ("acked", "manual"))
        self.assertFalse(document.move_ids)
        self.assertFalse(self.env["stock.move"].search([("product_id", "=", nope.id)]))

        # OpenMES lost the ack and lists it again: still only acknowledged.
        with self.mock_openmes(self._page([self._error_row()]), self._ack()) as calls:
            self._run()
        self.assertEqual(calls[1]["json"], {"erp_reference": "Not booked in Odoo"})
        self.assertEqual(document.state, "acked")
        self.assertFalse(document.move_ids)

    def test_acknowledge_without_booking_timeout(self):
        document = self._error_document()
        self._create_nope()

        def timeout(method, url, **kwargs):
            raise requests.Timeout("slow")

        with self.mock_openmes(timeout):
            document.action_acknowledge_without_booking()
        self.assertEqual((document.state, document.handling, document.attempt_count), ("booked", "manual", 1))
        # Retry re-sends the ack only: no backlog read, no booking.
        with self.mock_openmes(self._ack()) as calls:
            document.action_retry()
        self.assertEqual([c["method"] for c in calls], ["POST"])
        self.assertEqual(calls[0]["json"], {"erp_reference": "Not booked in Odoo"})
        self.assertEqual((document.state, document.handling), ("acked", "manual"))
        self.assertFalse(document.move_ids)

    def test_acknowledge_without_booking_404(self):
        document = self._error_document()
        self._create_nope()
        with self.mock_openmes(self._ack(404)):
            document.action_acknowledge_without_booking()
        self.assertEqual((document.state, document.handling, document.attempt_count), ("booked", "manual", 5))
        with self.mock_openmes(self._page([self._error_row()])) as calls:
            self._run()
        self.assertEqual(len(calls), 1, "not re-sent by the cron")
        self.assertFalse(document.move_ids)

    def test_retry_does_not_book_a_document_acked_in_openmes(self):
        document = self._error_document()
        self._create_nope()
        # Booked by hand and acknowledged in OpenMES meanwhile: gone from the backlog.
        with self.mock_openmes(self._page([])) as calls:
            result = document.action_retry()
        self.assertEqual([c["method"] for c in calls], ["GET"])
        self.assertEqual((document.state, document.handling), ("acked", "external"))
        self.assertFalse(document.move_ids)
        self.assertEqual(result["params"]["type"], "success")

    def test_retry_books_a_document_still_on_the_backlog(self):
        document = self._error_document()
        nope = self._create_nope()
        # The server corrected the quantity meanwhile: the refreshed lines are booked.
        with self.mock_openmes(self._page([self._error_row(qty=3)]), self._ack()) as calls, \
                self._record_commits(calls):
            result = document.action_retry()
        self.assertEqual([c["method"] for c in calls], ["GET", "COMMIT", "POST"])
        self.assertEqual((document.state, document.handling), ("acked", "moves"))
        self.assertEqual(len(document.move_ids), 1)
        self.assertEqual(document.move_ids.product_id, nope)
        self.assertEqual(document.move_ids.quantity, 3)
        self.assertEqual(result["params"]["type"], "success")

    def test_retry_backlog_unreadable_books_nothing(self):
        def timeout(method, url, **kwargs):
            raise requests.Timeout("slow")

        booked_row = self._doc(53, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        with self.mock_openmes(self._page([self._error_row(), booked_row]), self._ack(500)):
            self._run()
        document, booked = self._document(50), self._document(53)
        self.assertEqual((document.state, booked.state), ("error", "booked"))
        self._create_nope()
        for answer in (FakeResponse(500, {"message": "down"}), FakeResponse(403, {"message": "scope"}), timeout):
            with self.mock_openmes(answer) as calls:
                result = document.action_retry()
            self.assertEqual(len(calls), 1)
            self.assertEqual(document.state, "error")
            self.assertIn("NOPE-01", document.error_message, "the booking error is kept")
            self.assertFalse(document.move_ids)
            self.assertEqual(result["params"]["type"], "warning")
            self.assertIn("could not be read", result["params"]["message"])

        # A booked document in the same selection still gets its ack.
        with self.mock_openmes(FakeResponse(500, {"message": "down"}), self._ack()) as calls:
            (document | booked).action_retry()
        self.assertEqual([self._path(c) for c in calls], ["/stock-documents", "/stock-documents/53/ack"])
        self.assertEqual(booked.state, "acked")
        self.assertEqual(document.state, "error")
        self.assertFalse(document.move_ids)

    def test_retry_with_malformed_backlog_row_keeps_missing_document(self):
        document = self._error_document()
        self._create_nope()
        with self.mock_openmes(self._page(["junk"])) as calls:
            result = document.action_retry()
        self.assertEqual(len(calls), 1)
        self.assertEqual(document.state, "error", "not booked and not given up as acknowledged elsewhere")
        self.assertFalse(document.move_ids)
        self.assertEqual(result["params"]["type"], "warning")

    def test_retry_multi_company(self):
        company_b = self.env["res.company"].create({"name": "Bakery B"})
        self._configure(company_b, enabled=True, api_url="https://mes-b.example.test", api_key="omk_" + "b" * 40)
        warehouse_b = self.env["stock.warehouse"].search([("company_id", "=", company_b.id)], limit=1)
        warehouse_b.openmes_code = "0900"
        doc = self._doc(54, "material_issue", [self._line("NOPE-01", 2, "kg")], wh="B-1", erp="0900")
        with self.mock_openmes(self._page([doc])):
            self.Document.with_company(company_b)._openmes_sync_documents()
        document = self._document(54)
        self.assertEqual((document.company_id, document.state), (company_b, "error"))
        self._create_nope()
        with self.mock_openmes(self._page([doc]), self._ack()) as calls:
            document.action_retry()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["url"].startswith("https://mes-b.example.test/") for call in calls))
        self.assertEqual(document.state, "acked")
        self.assertEqual(document.move_ids.company_id, company_b)
        self.assertEqual(self.Document.search_count([("mes_id", "=", 54)]), 1, "no copy in company A")

    def test_booked_and_acked_documents_cannot_be_deleted(self):
        booked = self._doc(55, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        acked = self._doc(56, "material_receipt", [self._line("FLOUR-01", 1, "kg")])
        def ack(method, url, **kwargs):
            return self._ack(500 if url.endswith("/55/ack") else 200)

        with self.mock_openmes(self._page([booked, acked]), ack, ack):
            self._run()
        booked, acked = self._document(55), self._document(56)
        self.assertEqual((booked.state, acked.state), ("booked", "acked"))
        admin = new_test_user(self.env, "sd_admin", groups="base.group_system,stock.group_stock_manager")
        stock_manager = new_test_user(self.env, "sd_unlink_mgr", groups="stock.group_stock_manager")
        for document in (booked, acked):
            for env_document in (document, document.sudo(), document.with_user(admin)):
                with self.assertRaises(UserError):
                    env_document.unlink()
            with self.assertRaises(AccessError):
                document.with_user(stock_manager).unlink()
        self.assertEqual(len(booked.exists() | acked.exists()), 2)

        # Still on the backlog: not imported and booked a second time.
        with self.mock_openmes(self._page([self._doc(55, "material_receipt", [self._line("FLOUR-01", 1, "kg")])]),
                               self._ack()):
            self._run()
        self.assertEqual(len(booked.move_ids), 1)
        self.assertEqual(self._on_hand(self.flour), 2)

        # An unbooked document can go; the next run imports it again.
        document = self._error_document()
        document.with_user(admin).unlink()
        self.assertFalse(document.exists())
        with self.mock_openmes(self._page([self._error_row()])):
            self._run()
        self.assertEqual(self._document(50).state, "error")
