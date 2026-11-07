from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import requests

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from .common import FakeResponse, OpenmesCase, import_report


def no_documents():
    return FakeResponse(200, {"data": [], "meta": {"next_cursor": None}})


@tagged("post_install", "-at_install")
class TestMaterialLots(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.wh1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.wh1.openmes_code = "0100"
        cls.wh2 = cls.env["stock.warehouse"].create({"name": "Second", "code": "WH2", "openmes_code": "0200"})
        cls.flour.tracking = "lot"
        cls.Quant = cls.env["stock.quant"]
        cls.Sent = cls.env["openmes.material.lot.sent"]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _lot(self, product, name, ref=False):
        return self.env["stock.lot"].create({
            "name": name, "product_id": product.id, "company_id": self.env.company.id, "ref": ref,
        })

    def _put(self, product, location, qty, lot):
        self.Quant._update_available_quantity(product, location, qty, lot_id=lot)

    def _sync(self, *responses):
        with self.mock_openmes(no_documents(), *responses) as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()
        return calls, logs

    @staticmethod
    def _raise_timeout(*_args, **_kwargs):
        raise requests.Timeout("slow")

    @staticmethod
    def _path(call):
        return call["url"].split("/api/v1/erp", 1)[1]

    @staticmethod
    def _lots(call):
        return {(r["material_code"], r["lot_number"]): r for r in call["json"]["lots"]}

    def _lot_log(self, logs):
        log = logs.filtered(lambda l: l.operation == "material_lots")
        self.assertEqual(len(log), 1)
        return log

    def _sent_keys(self, warehouse=None):
        domain = [("warehouse_id", "=", warehouse.id)] if warehouse else []
        return set(self.Sent.search(domain).mapped(lambda s: (s.material_code, s.lot_number)))

    # ── Happy path ───────────────────────────────────────────────────────────

    def test_two_materials_two_warehouses(self):
        shelf = self.env["stock.location"].create({"name": "Shelf", "location_id": self.wh1.lot_stock_id.id})
        f1 = self._lot(self.flour, "F-1", ref="SUP-99")
        y1 = self._lot(self.yeast, "Y-1")
        f2 = self._lot(self.flour, "F-2")
        self._put(self.flour, self.wh1.lot_stock_id, 30, f1)
        self._put(self.flour, shelf, 12.5, f1)
        self._put(self.yeast, self.wh1.lot_stock_id, 2, y1)
        self._put(self.flour, self.wh2.lot_stock_id, 7, f2)
        self._put(self.yeast, self.wh2.lot_stock_id, 1, y1)

        calls, logs = self._sync(
            import_report(updated=2), import_report(updated=4), import_report(updated=2), import_report(updated=4)
        )

        self.assertEqual(
            [(self._path(c), c["json"]["warehouse_code"]) for c in calls[1:]],
            [("/material-lots/import", "0100"), ("/stock/import", "0100"),
             ("/material-lots/import", "0200"), ("/stock/import", "0200")],
            "lots go before the snapshot of the same warehouse",
        )
        lots1 = self._lots(calls[1])
        self.assertEqual(set(lots1), {("FLOUR-01", "F-1"), ("YEAST-01", "Y-1")})
        self.assertEqual(lots1["FLOUR-01", "F-1"], {
            "material_code": "FLOUR-01", "lot_number": "F-1", "quantity_available": 42.5,
            "unit_of_measure": self.uom_kg.name, "supplier_lot_no": "SUP-99",
        }, "quants of one lot in sub-locations add up")
        self.assertNotIn("supplier_lot_no", lots1["YEAST-01", "Y-1"])
        self.assertNotIn("expiry_date", lots1["YEAST-01", "Y-1"], "product_expiry is not installed")
        lots2 = self._lots(calls[3])
        self.assertEqual(set(lots2), {("FLOUR-01", "F-2"), ("YEAST-01", "Y-1")})
        self.assertEqual(lots2["FLOUR-01", "F-2"]["quantity_available"], 7)
        self.assertEqual(lots2["YEAST-01", "Y-1"]["quantity_available"], 1)

        log = self._lot_log(logs)
        self.assertEqual((log.state, log.sent_count, log.updated_count), ("success", 4, 4))
        self.assertEqual(len(logs.filtered(lambda l: l.operation == "stock")), 2)
        self.assertEqual(self._sent_keys(self.wh1), {("FLOUR-01", "F-1"), ("YEAST-01", "Y-1")})
        self.assertEqual(self._sent_keys(self.wh2), {("FLOUR-01", "F-2"), ("YEAST-01", "Y-1")})

    def test_untracked_material_sends_no_lots(self):
        self.flour.tracking = "none"
        self.Quant._update_available_quantity(self.flour, self.wh1.lot_stock_id, 50)
        calls, logs = self._sync(import_report(updated=4), import_report(updated=4))
        self.assertEqual([self._path(c) for c in calls], ["/stock-documents", "/stock/import", "/stock/import"])
        self.assertFalse(logs.filtered(lambda l: l.operation == "material_lots"))

    def test_untracked_material_is_left_out_of_the_lots(self):
        self.flour.tracking = "none"
        self.Quant._update_available_quantity(self.flour, self.wh1.lot_stock_id, 50)
        self._put(self.yeast, self.wh1.lot_stock_id, 3, self._lot(self.yeast, "Y-1"))
        calls, _logs = self._sync(import_report(updated=1), import_report(updated=4), import_report(updated=4))
        self.assertEqual(set(self._lots(calls[1])), {("YEAST-01", "Y-1")})

    def test_negative_lot_is_not_sent(self):
        self._put(self.yeast, self.wh1.lot_stock_id, -2, self._lot(self.yeast, "Y-1"))
        calls, _logs = self._sync(import_report(updated=4), import_report(updated=4))
        self.assertNotIn("/material-lots/import", [self._path(c) for c in calls])
        self.assertFalse(self.Sent.search([]))

    # ── Expiry date ──────────────────────────────────────────────────────────

    def test_expiry_date_from_product_expiry(self):
        lot = self._lot(self.yeast, "Y-1")
        if "expiration_date" not in lot._fields:
            self.skipTest("product_expiry is not installed")
        lot.expiration_date = datetime(2026, 12, 31, 10, 0)
        self._put(self.yeast, self.wh1.lot_stock_id, 3, lot)
        calls, _logs = self._sync(import_report(updated=1), import_report(updated=4), import_report(updated=4))
        self.assertEqual(self._lots(calls[1])["YEAST-01", "Y-1"]["expiry_date"], "2026-12-31")

    def test_lot_expiry_in_the_warehouse_time_zone(self):
        sync = self.env["openmes.sync"]
        fake = SimpleNamespace(_fields={"expiration_date": None}, expiration_date=datetime(2026, 12, 30, 23, 30))
        self.wh1.partner_id.tz = "Europe/Warsaw"
        tz = sync._lot_time_zone(self.wh1)
        self.assertEqual(sync._lot_expiry(fake, tz), "2026-12-31")
        # Not the user's: the cron and the button send the same date.
        self.assertEqual(sync.with_context(tz="America/New_York")._lot_time_zone(self.wh1).zone, "Europe/Warsaw")
        self.assertEqual(sync._lot_expiry(fake), "2026-12-30", "UTC by default")
        fake.expiration_date = False
        self.assertIsNone(sync._lot_expiry(fake, tz), "a lot without a date")

    def test_lot_time_zone_falls_back_to_the_company_then_utc(self):
        sync = self.env["openmes.sync"]
        self.wh2.partner_id = False
        self.env.company.partner_id.tz = "Asia/Tokyo"
        self.assertEqual(sync._lot_time_zone(self.wh2).zone, "Asia/Tokyo")
        self.env.company.partner_id.tz = False
        self.assertEqual(sync._lot_time_zone(self.wh2).zone, "UTC")

    def test_lot_expiry_without_product_expiry(self):
        fake = SimpleNamespace(_fields={})
        self.assertIsNone(self.env["openmes.sync"]._lot_expiry(fake), "no field, no date, no error")

    def test_expiry_date_is_sent_when_known(self):
        self._put(self.yeast, self.wh1.lot_stock_id, 3, self._lot(self.yeast, "Y-1"))
        with patch.object(type(self.env["openmes.sync"]), "_lot_expiry", lambda self, lot, tz=None: "2027-01-15"):
            calls, _logs = self._sync(import_report(updated=1), import_report(updated=4), import_report(updated=4))
        self.assertEqual(self._lots(calls[1])["YEAST-01", "Y-1"]["expiry_date"], "2027-01-15")

    # ── A lot used up is sent once with 0 ────────────────────────────────────

    def test_lot_used_up_is_sent_with_zero_once(self):
        lot = self._lot(self.flour, "L1")
        self._put(self.flour, self.wh1.lot_stock_id, 10, lot)
        calls, _logs = self._sync(import_report(imported=1), import_report(updated=4), import_report(updated=4))
        self.assertEqual(self._lots(calls[1])["FLOUR-01", "L1"]["quantity_available"], 10)
        self.assertEqual(self._sent_keys(self.wh1), {("FLOUR-01", "L1")})

        self._put(self.flour, self.wh1.lot_stock_id, -10, lot)
        calls, logs = self._sync(import_report(updated=1), import_report(updated=4), import_report(updated=4))
        self.assertEqual(self._path(calls[1]), "/material-lots/import")
        self.assertEqual(calls[1]["json"]["lots"], [{
            "material_code": "FLOUR-01", "lot_number": "L1", "quantity_available": 0.0,
        }], "no unit on a 0: it cannot fail on a unit changed in OpenMES")
        self.assertEqual(self._lot_log(logs).state, "success")
        self.assertFalse(self.Sent.search([]), "forgotten once OpenMES took the 0")

        calls, _logs = self._sync(import_report(updated=4), import_report(updated=4))
        self.assertNotIn("/material-lots/import", [self._path(c) for c in calls])

    def test_zero_is_sent_after_the_material_left_the_lot_sync(self):
        # Documented: the lots of a material no longer sent are set to 0 in
        # OpenMES, as nothing would keep them current any more.
        lot = self._lot(self.flour, "L1")
        self._put(self.flour, self.wh1.lot_stock_id, 10, lot)
        self._sync(import_report(imported=1), import_report(updated=4), import_report(updated=4))
        self.flour.tracking = "none"
        calls, _logs = self._sync(import_report(updated=1), import_report(updated=4), import_report(updated=4))
        self.assertEqual(calls[1]["json"]["lots"][0]["quantity_available"], 0)

    def test_rejected_zero_is_kept_for_the_next_sync(self):
        lot = self._lot(self.flour, "L1")
        self._put(self.flour, self.wh1.lot_stock_id, 10, lot)
        self._sync(import_report(imported=1), import_report(updated=4), import_report(updated=4))
        self._put(self.flour, self.wh1.lot_stock_id, -10, lot)
        refused = import_report(errors=[{"row": 1, "field": "lot_number", "message": "Lot is locked"}])
        _calls, logs = self._sync(refused, import_report(updated=4), import_report(updated=4))
        log = self._lot_log(logs)
        self.assertEqual(log.state, "partial")
        self.assertIn("[0100] FLOUR-01 / L1: lot_number: Lot is locked", log.message)
        self.assertEqual(self._sent_keys(self.wh1), {("FLOUR-01", "L1")})

    def test_permanently_refused_zero_is_given_up(self):
        lot = self._lot(self.flour, "L1")
        self._put(self.flour, self.wh1.lot_stock_id, 10, lot)
        self.wh2.openmes_code = False
        self._sync(import_report(imported=1), import_report(updated=4))
        self._put(self.flour, self.wh1.lot_stock_id, -10, lot)

        def refused():
            return import_report(errors=[{"row": 1, "field": "lot_number", "message": "Lot is locked"}])

        for run in (1, 2):
            _calls, logs = self._sync(refused(), import_report(updated=4))
            self.assertEqual(self.Sent.search([]).zero_refusals, run)
            self.assertNotIn("no longer sent", self._lot_log(logs).message)
        _calls, logs = self._sync(refused(), import_report(updated=4))
        message = self._lot_log(logs).message
        self.assertIn("[0100] FLOUR-01 / L1: lot_number: Lot is locked", message)
        self.assertIn("[0100] FLOUR-01 / L1: the 0 was refused 3 times and is no longer sent", message)
        self.assertFalse(self.Sent.search([]))
        calls, _logs = self._sync(import_report(updated=4))
        self.assertNotIn("/material-lots/import", [self._path(c) for c in calls], "no more noise")

    def test_lot_back_in_stock_resets_the_refusals(self):
        lot = self._lot(self.flour, "L1")
        self._put(self.flour, self.wh1.lot_stock_id, 10, lot)
        self.wh2.openmes_code = False
        self._sync(import_report(imported=1), import_report(updated=4))
        self._put(self.flour, self.wh1.lot_stock_id, -10, lot)
        refused = import_report(errors=[{"row": 1, "message": "Lot is locked"}])
        self._sync(refused, import_report(updated=4))
        self.assertEqual(self.Sent.search([]).zero_refusals, 1)
        self._put(self.flour, self.wh1.lot_stock_id, 4, lot)
        self._sync(import_report(updated=1), import_report(updated=4))
        self.assertEqual(self.Sent.search([]).zero_refusals, 0)

    def test_error_without_row_keeps_every_lot(self):
        old = self._lot(self.flour, "L1")
        self._put(self.flour, self.wh1.lot_stock_id, 10, old)
        self.wh2.openmes_code = False
        self._sync(import_report(imported=1), import_report(updated=4))
        self._put(self.flour, self.wh1.lot_stock_id, -10, old)
        self._put(self.yeast, self.wh1.lot_stock_id, 2, self._lot(self.yeast, "Y-1"))
        unplaced = import_report(updated=1, errors=[{"message": "Something went wrong"}])
        _calls, logs = self._sync(unplaced, import_report(updated=4))
        self.assertIn("[0100] ?: Something went wrong", self._lot_log(logs).message)
        self.assertEqual(self._sent_keys(), {("FLOUR-01", "L1"), ("YEAST-01", "Y-1")},
                         "a new lot OpenMES may hold and a 0 that may have failed are both kept")
        self.assertEqual(self.Sent.search([("lot_number", "=", "L1")]).zero_refusals, 0)

    def test_unit_mismatch_is_readable_and_not_remembered(self):
        self._put(self.flour, self.wh1.lot_stock_id, 10, self._lot(self.flour, "L1"))
        self._put(self.yeast, self.wh1.lot_stock_id, 1, self._lot(self.yeast, "Y-1"))
        refused = import_report(updated=1, errors=[{
            "row": 1, "field": "unit_of_measure",
            "message": "Unit 'kg' does not match the unit of 'FLOUR-01' (g).",
        }])
        _calls, logs = self._sync(refused, import_report(updated=4), import_report(updated=4))
        log = self._lot_log(logs)
        self.assertEqual((log.state, log.error_count), ("partial", 1))
        self.assertEqual(
            log.message, "[0100] FLOUR-01 / L1: unit_of_measure: Unit 'kg' does not match the unit of 'FLOUR-01' (g)."
        )
        self.assertEqual(self._sent_keys(), {("YEAST-01", "Y-1")}, "OpenMES does not hold the refused lot")

    # ── Blocking ─────────────────────────────────────────────────────────────

    def test_unbooked_document_blocks_lots_of_its_warehouse(self):
        self._put(self.flour, self.wh1.lot_stock_id, 10, self._lot(self.flour, "L1"))
        self._put(self.flour, self.wh2.lot_stock_id, 4, self._lot(self.flour, "L2"))
        # A document OpenMES posted for an unknown material cannot be booked.
        document = {
            "id": 999, "document_no": "DOC/2026/0999", "type": "material_issue", "status": "posted",
            "direction": "out", "warehouse_code": "RAW-1", "warehouse_erp_code": "0100",
            "work_order_no": None, "erp_reference": None, "erp_synced_at": None,
            "posted_at": "2026-11-03T18:37:13+00:00",
            "lines": [{"material_code": "NOPE-01", "product_type_code": None, "lot_number": None,
                       "quantity": 1, "unit_of_measure": "kg", "notes": None}],
        }
        page = FakeResponse(200, {"data": [document], "meta": {"next_cursor": None}})
        with self.mock_openmes(page, import_report(updated=1), import_report(updated=4)) as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual(self.env["openmes.stock.document"].search([("mes_id", "=", 999)]).state, "error")
        self.assertEqual(
            [(self._path(c), c["json"]["warehouse_code"]) for c in calls[1:]],
            [("/material-lots/import", "0200"), ("/stock/import", "0200")],
        )
        self.assertEqual(self._sent_keys(), {("FLOUR-01", "L2")})
        self.assertEqual(self._lot_log(logs).sent_count, 1)

    def test_unreadable_documents_send_no_lots(self):
        self._put(self.flour, self.wh1.lot_stock_id, 10, self._lot(self.flour, "L1"))
        with self.mock_openmes(FakeResponse(403, {"message": "scope"})) as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual(len(calls), 1)
        self.assertFalse(logs.filtered(lambda l: l.operation == "material_lots"))
        self.assertFalse(self.Sent.search([]))

    # ── OpenMES errors ───────────────────────────────────────────────────────

    def _http_error_case(self, answer):
        self._put(self.flour, self.wh1.lot_stock_id, 10, self._lot(self.flour, "L1"))
        self.wh2.openmes_code = False
        with mute_logger("odoo.addons.openmes_connector.models.openmes_client"):
            calls, logs = self._sync(answer, import_report(updated=4))
        self.assertEqual([self._path(c) for c in calls[1:]], ["/material-lots/import", "/stock/import"],
                         "the snapshot is still sent")
        stock = logs.filtered(lambda l: l.operation == "stock")
        self.assertEqual(stock.state, "success")
        log = self._lot_log(logs)
        self.assertEqual(log.state, "failed")
        self.assertTrue(log.message.startswith("[0100] "))
        return log

    def test_server_error(self):
        self._http_error_case(FakeResponse(500, {"message": "boom"}))
        self.assertEqual(self._sent_keys(), {("FLOUR-01", "L1")}, "OpenMES may have written it")

    def test_timeout(self):
        self._http_error_case(self._raise_timeout)
        self.assertEqual(self._sent_keys(), {("FLOUR-01", "L1")}, "remembered before it was sent")

    def test_missing_scope(self):
        log = self._http_error_case(FakeResponse(403, {"message": "Forbidden"}))
        self.assertIn("scope", log.message)

    def test_timeout_keeps_the_zero(self):
        lot = self._lot(self.flour, "L1")
        self._put(self.flour, self.wh1.lot_stock_id, 10, lot)
        self.wh2.openmes_code = False
        self._sync(import_report(imported=1), import_report(updated=4))
        self._put(self.flour, self.wh1.lot_stock_id, -10, lot)
        with mute_logger("odoo.addons.openmes_connector.models.openmes_client"):
            self._sync(self._raise_timeout, import_report(updated=4))
        self.assertEqual(self._sent_keys(), {("FLOUR-01", "L1")})

    # ── Access rights ────────────────────────────────────────────────────────

    def test_sync_stock_button(self):
        self._put(self.flour, self.wh1.lot_stock_id, 10, self._lot(self.flour, "L1"))
        self.wh2.openmes_code = False
        with self.mock_openmes(no_documents(), import_report(imported=1), import_report(updated=4)) as calls:
            action = self.env["res.config.settings"].create({}).action_openmes_sync_stock()
        self.assertEqual(self._path(calls[1]), "/material-lots/import")
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(self._sent_keys(), {("FLOUR-01", "L1")})

    def test_users_cannot_read_remembered_lots(self):
        self._put(self.flour, self.wh1.lot_stock_id, 10, self._lot(self.flour, "L1"))
        self.wh2.openmes_code = False
        self._sync(import_report(imported=1), import_report(updated=4))
        sent = self.Sent.search([])
        self.assertTrue(sent)
        for login, groups in (
            ("lot_salesman", "sales_team.group_sale_salesman"),
            ("lot_stock_user", "stock.group_stock_user"),
            ("lot_stock_manager", "stock.group_stock_manager,mrp.group_mrp_manager"),
        ):
            user = new_test_user(self.env, login, groups=groups)
            with self.assertRaises(AccessError):
                sent.with_user(user).read(["lot_number"])
