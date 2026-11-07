import requests

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from .common import API_URL, FakeResponse, OpenmesCase, import_report, lines_page

LINE_LOGGER = "odoo.addons.openmes_connector.models.openmes_line"
CLIENT_LOGGER = "odoo.addons.openmes_connector.models.openmes_client"


def row(code, name=None, active=True, **extra):
    return dict({"code": code, "name": name or code, "description": None, "is_active": active,
                 "updated_at": "2026-11-02T08:15:00+00:00"}, **extra)


@tagged("post_install", "-at_install")
class TestLines(OpenmesCase):
    @property
    def Line(self):
        return self.env["openmes.line"].with_context(active_test=False)

    def _line(self, code):
        return self.Line.search([("code", "=", code)])

    def _refresh(self, *responses):
        with self.mock_openmes(*responses) as calls:
            log = self.env["openmes.line"]._openmes_refresh()
        return log, calls

    # ── Refresh ──────────────────────────────────────────────────────────────

    def test_refresh_creates_updates_and_deactivates(self):
        log, calls = self._refresh(
            lines_page([row("L1", "Oven line"), row("L3", active=False)], next_cursor="c2"),
            lines_page([row("L4", "Packing", description="Hall B")]),
        )
        self.assertEqual(self._line("L1").name, "Oven line")
        self.assertTrue(self._line("L1").active)
        self.assertEqual(str(self._line("L1").openmes_updated_at), "2026-11-02 08:15:00")
        self.assertTrue(self._line("L3"))
        self.assertFalse(self._line("L3").active)
        self.assertEqual(self._line("L4").name, "Packing")
        self.assertEqual(self._line("L4").description, "Hall B")
        self.assertFalse(self._line("L2").active, "missing from the list = deleted in OpenMES")

        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["url"], f"{API_URL}/api/v1/erp/lines")
        self.assertEqual(calls[0]["params"], {"limit": 500})
        self.assertEqual(calls[1]["params"], {"limit": 500, "cursor": "c2"})
        self.assertNotIn("since", calls[1]["params"])

        self.assertEqual(log.operation, "lines")
        self.assertEqual(log.state, "success")
        self.assertEqual(
            (log.sent_count, log.imported_count, log.updated_count, log.skipped_count, log.error_count),
            (3, 2, 1, 1, 0),
        )

    def test_refresh_reactivates_a_line(self):
        page = [row("L1", "Line one"), row("L2", "Line two")]
        self._refresh(lines_page(page))
        self.line_l2.active = False
        log, _calls = self._refresh(lines_page(page))
        self.assertTrue(self.line_l2.active)
        self.assertEqual(log.updated_count, 1)
        self.assertEqual(log.imported_count, 0)

    def test_refresh_is_idempotent(self):
        page = [row("L1", "Line one"), row("L2", "Line two"), row("L5")]
        self._refresh(lines_page(page))
        log, _calls = self._refresh(lines_page(page))
        self.assertEqual((log.imported_count, log.updated_count, log.skipped_count), (0, 0, 0))
        self.assertEqual(len(self._line("L5")), 1)

    def test_refresh_errors_change_nothing(self):
        cases = {
            "403": [FakeResponse(403, {"message": "Forbidden"})],
            "500": [FakeResponse(500, None)],
            "timeout": [lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow"))],
            "html": [FakeResponse(200, None)],
            "page 2": [lines_page([row("L1", "Renamed"), row("L9")], next_cursor="c2"), FakeResponse(500, None)],
        }
        for case, responses in cases.items():
            with self.subTest(case=case), mute_logger(CLIENT_LOGGER, LINE_LOGGER):
                log, _calls = self._refresh(*responses)
                self.assertEqual(log.state, "failed")
                self.assertTrue(log.message)
                self.assertEqual(self.line_l1.name, "Line one")
                self.assertTrue(self.line_l1.active and self.line_l2.active)
                self.assertFalse(self._line("L9"))

    def test_refresh_skips_bad_rows(self):
        log, _calls = self._refresh(lines_page([
            {"name": "no code"},
            row(" L7"),
            row("X" * 101),
            row(17),
            "not a row",
            row("L6", name=["not", "a", "string"], active="yes"),
            row("L1", "Line one"),
            row("L2", "Line two"),
        ]))
        self.assertEqual(log.state, "partial")
        self.assertEqual(log.error_count, 5)
        self.assertEqual(self._line("L6").name, "L6", "a bad name falls back to the code")
        self.assertTrue(self._line("L6").active)
        self.assertFalse(self.Line.search([("code", "in", ["L7", " L7", "X" * 101])]))
        self.assertTrue(self.line_l1.active and self.line_l2.active)

    def test_deactivated_line_is_still_sent(self):
        with self.mock_openmes(lines_page([row("L1", "Line one")])):
            self.env["openmes.line"]._openmes_refresh()
        self.assertFalse(self.line_l2.active)
        self.assertEqual(self.cake.product_tmpl_id.openmes_line_id, self.line_l2)
        order = self._create_order([(self.cake, 1, {})])
        order.action_confirm()
        with self.mock_openmes(import_report(imported=1)) as calls:
            order.openmes_work_order_ids._openmes_send()
        self.assertEqual(calls[0]["json"]["orders"][0]["line_code"], "L2")

    def test_cron(self):
        self._configure(enabled=False)
        with self.mock_openmes():
            self.env["openmes.line"]._cron_openmes_refresh_lines()
        self._configure(enabled=True)
        with self.mock_openmes(lines_page([row("L1"), row("L2"), row("L8")])):
            self.env["openmes.line"]._cron_openmes_refresh_lines()
        self.assertTrue(self._line("L8"))
        cron = self.env.ref("openmes_connector.ir_cron_openmes_refresh_lines")
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "days"))

    # ── Constraints ──────────────────────────────────────────────────────────

    def test_line_code_constraint(self):
        for code in (" L1x", "L1x ", "L" * 101):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                self.env["openmes.line"].create({"code": code, "name": "x"})

    def test_display_name(self):
        self.assertEqual(self.line_l1.display_name, "[L1] Line one")
        self.assertEqual(self.env["openmes.line"].create({"code": "L5", "name": "L5"}).display_name, "L5")
        found = self.env["openmes.line"].name_search("Line two")
        self.assertEqual([r[0] for r in found], [self.line_l2.id])

    def test_unlink_protection(self):
        with self.assertRaises(UserError):
            self.line_l1.unlink()
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self.line_l2.unlink()
        spare = self.env["openmes.line"].create({"code": "L5", "name": "Spare"})
        spare.unlink()
        self.assertFalse(spare.exists())

    # ── Access ───────────────────────────────────────────────────────────────

    def test_access_rights(self):
        salesman = new_test_user(self.env, "om_line_salesman", groups="sales_team.group_sale_salesman")
        lines = self.env["openmes.line"].with_user(salesman)
        self.assertEqual(lines.browse(self.line_l1.id).name, "Line one")
        with self.assertRaises(AccessError):
            lines.browse(self.line_l1.id).write({"name": "x"})
        with self.assertRaises(AccessError):
            lines.create({"code": "L5", "name": "x"})
        with self.assertRaises(AccessError):
            lines.browse(self.line_l2.id).unlink()

        manager = new_test_user(self.env, "om_line_manager", groups="sales_team.group_sale_manager")
        settings = self.env["res.config.settings"].create({})
        with self.assertRaises(AccessError):
            settings.with_user(manager).action_openmes_refresh_lines()

        with self.mock_openmes(lines_page([row("L1", "Line one"), row("L2", "Line two")])):
            result = settings.action_openmes_refresh_lines()
        self.assertEqual(result["params"]["type"], "success")
        self.assertIn("2 fetched", result["params"]["message"])

    def test_multi_company(self):
        company_a = self.env.company
        company_b = self.env["res.company"].create({"name": "Plant B"})
        line_b = self.env["openmes.line"].create({"code": "LB", "name": "Plant B line", "company_id": company_b.id})
        user_a = new_test_user(self.env, "om_line_user_a", groups="base.group_user",
                               company_id=company_a.id, company_ids=[(6, 0, company_a.ids)])
        user_b = new_test_user(self.env, "om_line_user_b", groups="base.group_user",
                               company_id=company_b.id, company_ids=[(6, 0, company_b.ids)])
        seen_a = self.env["openmes.line"].with_user(user_a).search([]).mapped("code")
        seen_b = self.env["openmes.line"].with_user(user_b).search([]).mapped("code")
        self.assertNotIn("LB", seen_a)
        self.assertIn("L1", seen_a)
        self.assertIn("LB", seen_b)
        self.assertNotIn("L1", seen_b, "a line belongs to the OpenMES instance of one company")
        tmpl = self.bread.product_tmpl_id
        tmpl.company_id = company_a
        with self.assertRaises(UserError):
            tmpl.openmes_line_id = line_b

    # ── Migration ────────────────────────────────────────────────────────────

    def test_adopt_legacy_codes(self):
        bread, flour, yeast = (p.product_tmpl_id for p in (self.bread, self.flour, self.yeast))
        service = self.service.product_tmpl_id
        count = self.Line.search_count([])
        Line = self.env["openmes.line"]
        Line._openmes_adopt_legacy_codes(
            {bread.id: "L9", flour.id: "L9 ", yeast.id: " ", service.id: "L1"}, "LD"
        )
        line_l9 = self._line("L9")
        self.assertEqual(len(line_l9), 1)
        self.assertEqual(line_l9.name, "L9")
        self.assertEqual(bread.openmes_line_id, line_l9)
        self.assertEqual(flour.openmes_line_id, line_l9)
        self.assertFalse(yeast.openmes_line_id, "a blank code is ignored")
        self.assertEqual(service.openmes_line_id, self.line_l1, "an existing line is reused")
        self.assertTrue(self._line("LD"))
        self.assertEqual(self.Line.search_count([]), count + 2)

        Line._openmes_adopt_legacy_codes({bread.id: "L9", service.id: "L1"}, "LD")
        self.assertEqual(self.Line.search_count([]), count + 2, "running it twice adds nothing")
        Line._openmes_adopt_legacy_codes({bread.id: "X" * 101}, False)
        self.assertEqual(bread.openmes_line_id, line_l9, "a too long code is not migrated")

    # ── Product line code (compatibility) ────────────────────────────────────

    def test_product_line_code_compatibility(self):
        tmpl = self.bread.product_tmpl_id
        tmpl.openmes_line_code = "L1"
        self.assertEqual(tmpl.openmes_line_id, self.line_l1)
        self.assertEqual(tmpl.openmes_line_code, "L1")
        self.assertEqual(self.cake.product_tmpl_id.openmes_line_code, "L2")
        found = self.env["product.template"].search([("openmes_line_code", "=", "L2")])
        self.assertIn(self.cake.product_tmpl_id, found)
        self.assertNotIn(tmpl, found)
        with self.assertRaises(ValidationError):
            tmpl.openmes_line_code = "NOPE"
        with self.assertRaises(ValidationError):
            tmpl.openmes_line_code = "L1 "
        tmpl.openmes_line_code = False
        self.assertFalse(tmpl.openmes_line_id)
