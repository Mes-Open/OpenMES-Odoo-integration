"""Odoo work centers → OpenMES workstations in the routing sync."""

import requests

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from .common import FakeResponse, OpenmesCase, import_report

CLIENT_LOGGER = "odoo.addons.openmes_connector.models.openmes_client"
ROUTINGS = "/process-templates/import"


def path_of(call):
    return call["url"].rsplit("/api/v1/erp", 1)[1]


@tagged("post_install", "-at_install")
class TestWorkstations(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Workcenter = cls.env["mrp.workcenter"]
        cls.oven = Workcenter.create({"name": "Oven", "openmes_workstation_code": "WS-OVEN", "capacity_ids": [(0, 0, {"capacity": 2})]})
        cls.table = Workcenter.create({"name": "Mixing table"})
        cls.bread_bom = cls.env["mrp.bom"].search([("product_tmpl_id", "=", cls.bread.product_tmpl_id.id)])
        cls.bread_bom.write({"operation_ids": [
            (0, 0, {"name": "Mixing", "workcenter_id": cls.table.id, "time_cycle_manual": 6, "sequence": 1}),
            (0, 0, {"name": "Baking", "workcenter_id": cls.oven.id, "time_cycle_manual": 40, "sequence": 2,
                    **cls._operation_note("<p>Bake at 220 °C</p>")}),
        ]})

    def _sync(self, *routing_answers):
        """Run the master data sync; answer the routing call(s) with ``routing_answers``."""
        answers = list(routing_answers) or [import_report(imported=1)]

        def answer(method, url, **kwargs):
            if url.endswith(ROUTINGS):
                response = answers.pop(0)
                return response(method, url, **kwargs) if callable(response) else response
            return import_report(imported=1)

        # products (default template) + products (routed) + routings + materials + boms
        responses = [answer] * 5
        with self.mock_openmes(*responses) as calls:
            logs = self.env["openmes.sync"]._sync_master_data()
        routings = [call["json"] for call in calls if path_of(call) == ROUTINGS]
        return logs, routings

    def _steps(self, routings, product_code):
        template = next(t for body in routings for t in body["templates"] if t["product_type_code"] == product_code)
        return template["steps"]

    # ── Payload ──────────────────────────────────────────────────────────────

    def test_step_gets_workstation_and_line_only_when_coded(self):
        logs, routings = self._sync()
        mixing, baking = self._steps(routings, "BREAD-01")
        self.assertNotIn("workstation_code", mixing)
        self.assertNotIn("line_code", mixing)
        self.assertEqual(baking["workstation_code"], "WS-OVEN")
        self.assertEqual(baking["line_code"], "L1", "the company's default line")
        # No capacity row on the mixing table: one cycle makes the whole BoM
        # quantity (10 breads), as Odoo 19 plans it.
        self.assertEqual(mixing["run_time_per_unit_minutes"], 0.6)
        # The rest of the step is unchanged.
        self.assertEqual((baking["step_number"], baking["name"], baking["run_time_per_unit_minutes"]),
                         (20, "Baking", 20.0))
        if self._has_operation_note():
            self.assertIn("220", baking["instruction"])
        else:
            self.assertNotIn("instruction", baking)
        self.assertEqual(logs.filtered(lambda l: l.operation == "routings").state, "success")

    def test_product_line_is_sent(self):
        self.env["mrp.bom"].create({
            "product_tmpl_id": self.cake.product_tmpl_id.id,
            "bom_line_ids": [(0, 0, {"product_id": self.flour.id, "product_qty": 1})],
            "operation_ids": [(0, 0, {"name": "Baking", "workcenter_id": self.oven.id, "time_cycle_manual": 30})],
        })
        with self.mock_openmes(*[import_report(imported=1)] * 4) as calls:
            self.env["openmes.sync"]._sync_master_data()
        routings = [call["json"] for call in calls if path_of(call) == ROUTINGS]
        self.assertEqual(self._steps(routings, "CAKE-01")[0]["line_code"], "L2")
        self.assertEqual(self._steps(routings, "BREAD-01")[1]["line_code"], "L1")

    def test_no_line_sends_workstation_alone(self):
        self.env.company.openmes_default_line_id = False
        _logs, routings = self._sync()
        baking = self._steps(routings, "BREAD-01")[1]
        self.assertEqual(baking["workstation_code"], "WS-OVEN")
        self.assertNotIn("line_code", baking)

    def test_too_long_line_code_is_left_out(self):
        long_line = self.env["openmes.line"].create({"code": "L" * 51, "name": "Long"})
        self.env.company.openmes_default_line_id = long_line
        _logs, routings = self._sync()
        baking = self._steps(routings, "BREAD-01")[1]
        self.assertEqual(baking["workstation_code"], "WS-OVEN")
        self.assertNotIn("line_code", baking)

    def test_no_coded_work_center_sends_no_workstation_keys(self):
        self.oven.openmes_workstation_code = False
        _logs, routings = self._sync()
        for step in self._steps(routings, "BREAD-01"):
            self.assertNotIn("workstation_code", step)
            self.assertNotIn("line_code", step)

    def test_changed_code_is_sent_at_next_sync(self):
        _logs, first = self._sync(import_report(imported=1, rows=[{"row": 1, "code": "BREAD-01", "version": 2}]))
        self.oven.openmes_workstation_code = "WS-OVEN2"
        _logs, second = self._sync(import_report(imported=1, rows=[{"row": 1, "code": "BREAD-01", "version": 3}]))
        self.assertEqual(self._steps(first, "BREAD-01")[1]["workstation_code"], "WS-OVEN")
        self.assertEqual(self._steps(second, "BREAD-01")[1]["workstation_code"], "WS-OVEN2")
        # Unchanged since: OpenMES skips it, which is a success.
        logs, third = self._sync(import_report(skipped=1))
        self.assertEqual(third, second)
        log = logs.filtered(lambda l: l.operation == "routings")
        self.assertEqual((log.state, log.skipped_count, log.error_count), ("success", 1, 0))

    # ── Validation ───────────────────────────────────────────────────────────

    def test_code_validation(self):
        Workcenter = self.env["mrp.workcenter"]
        for bad in ("W" * 51, " WS-1", "WS-1 "):
            with self.subTest(code=bad), self.assertRaises(ValidationError):
                Workcenter.create({"name": "Bad", "openmes_workstation_code": bad})
        self.assertEqual(Workcenter.create({"name": "Max", "openmes_workstation_code": "W" * 50})
                         .openmes_workstation_code, "W" * 50)
        self.assertFalse(Workcenter.create({"name": "Empty", "openmes_workstation_code": False})
                         .openmes_workstation_code)
        # Several work centers without a code are fine.
        Workcenter.create({"name": "Empty 2"})

        with self.assertRaises(ValidationError):
            Workcenter.create({"name": "Second oven", "openmes_workstation_code": "WS-OVEN"})
        with self.assertRaises(ValidationError):
            self.table.openmes_workstation_code = "WS-OVEN"
        self.oven.active = False
        with self.assertRaises(ValidationError):
            Workcenter.create({"name": "Second oven", "openmes_workstation_code": "WS-OVEN"})

    def test_shared_work_center_clashes_with_every_company(self):
        Workcenter = self.env["mrp.workcenter"]
        Workcenter.create({"name": "Shared", "openmes_workstation_code": "WS-SHARED", "company_id": False,
                          "resource_calendar_id": False})
        with self.assertRaises(ValidationError):
            Workcenter.create({"name": "Own", "openmes_workstation_code": "WS-SHARED"})
        with self.assertRaises(ValidationError):
            Workcenter.create({"name": "Shared 2", "openmes_workstation_code": "WS-OVEN", "company_id": False,
                              "resource_calendar_id": False})

    # ── Error reports ────────────────────────────────────────────────────────

    def _routing_log(self, error):
        logs, _routings = self._sync(import_report(errors=[error]))
        return logs.filtered(lambda l: l.operation == "routings")

    def test_unknown_workstation_names_the_work_center(self):
        log = self._routing_log({"row": 1, "field": "steps.1.workstation_code",
                                 "message": "Workstation 'WS-OVEN' not found"})
        self.assertEqual((log.state, log.error_count), ("partial", 1))
        self.assertIn("BREAD-01: steps.1.workstation_code: Workstation 'WS-OVEN' not found", log.message)
        self.assertIn("(Odoo work center: Oven)", log.message)

    def test_line_error_names_the_work_center_by_step(self):
        log = self._routing_log({"row": 1, "field": "steps.1.line_code", "message": "Line 'L1' not found"})
        self.assertEqual(log.state, "partial")
        self.assertIn("Line 'L1' not found", log.message)
        self.assertIn("(Odoo work center: Oven)", log.message)

    def test_other_error_is_logged_as_before(self):
        log = self._routing_log({"row": 1, "field": "product_type_code", "message": "Unknown product type"})
        self.assertEqual(log.message, "BREAD-01: product_type_code: Unknown product type")

    def test_api_failures_are_logged_not_raised(self):
        cases = {
            "500": FakeResponse(500, None),
            "403": FakeResponse(403, {"message": "Forbidden"}),
            "timeout": lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow")),
        }
        for case, response in cases.items():
            with self.subTest(case=case), mute_logger(CLIENT_LOGGER):
                calls = []
                answers = [response]

                def answer(method, url, **kwargs):
                    calls.append(url)
                    if url.endswith(ROUTINGS):
                        reply = answers.pop(0)
                        return reply(method, url, **kwargs) if callable(reply) else reply
                    return import_report(imported=1)

                with self.mock_openmes(*[answer] * 5):
                    logs = self.env["openmes.sync"]._sync_master_data()
                log = logs.filtered(lambda l: l.operation == "routings")
                self.assertEqual(log.state, "failed")
                self.assertTrue(log.message)
                paths = [url.rsplit("/api/v1/erp", 1)[1] for url in calls]
                self.assertIn("/materials/import", paths)
                self.assertIn("/boms/import", paths)

    # ── Access ───────────────────────────────────────────────────────────────

    def test_access(self):
        salesman = new_test_user(self.env, "om_ws_salesman", groups="sales_team.group_sale_salesman")
        with self.assertRaises(AccessError):
            self.oven.with_user(salesman).write({"openmes_workstation_code": "WS-X"})
        mrp_user = new_test_user(self.env, "om_ws_mrp_user", groups="mrp.group_mrp_user")
        with self.assertRaises(AccessError):
            self.oven.with_user(mrp_user).write({"openmes_workstation_code": "WS-X"})
        manager = new_test_user(self.env, "om_ws_mrp_manager", groups="mrp.group_mrp_manager")
        self.oven.with_user(manager).write({"openmes_workstation_code": "WS-X"})
        self.assertEqual(self.oven.openmes_workstation_code, "WS-X")
        # The admin sync reads it regardless of who saved it.
        _logs, routings = self._sync()
        self.assertEqual(self._steps(routings, "BREAD-01")[1]["workstation_code"], "WS-X")
