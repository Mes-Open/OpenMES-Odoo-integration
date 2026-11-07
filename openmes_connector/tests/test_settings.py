import requests

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from .common import API_KEY, API_URL, FakeResponse, OpenmesCase

CLIENT_LOGGER = "odoo.addons.openmes_connector.models.openmes_client"
ALL_SCOPES = (
    "erp:orders:import",
    "erp:production:read",
    "erp:masterdata:read",
    "erp:masterdata:write",
    "erp:stock:read",
    "erp:stock:write",
    "erp:webhooks:manage",
)


@tagged("post_install", "-at_install")
class TestSettings(OpenmesCase):
    def _settings(self, **vals):
        base = {
            "openmes_enabled": True,
            "openmes_api_url": API_URL,
            "openmes_api_key": API_KEY,
            "openmes_default_line_id": self.line_l1.id,
            "openmes_order_prefix": "ODOO",
        }
        base.update(vals)
        return self.env["res.config.settings"].create(base)

    def test_valid_settings_are_saved_normalised(self):
        self._settings(openmes_api_url=API_URL + "/", openmes_api_key=f" {API_KEY} ").execute()
        self.assertEqual(self.env.company.openmes_api_url, API_URL)
        self.assertEqual(self.env.company.openmes_api_key, API_KEY)

    def test_order_source_setting(self):
        self.assertEqual(self._settings().openmes_order_source, "sale_lines")
        self.assertEqual(self.env["openmes.client"]._order_source(), "sale_lines")
        self._settings(openmes_order_source="manufacturing_orders").execute()
        self.assertEqual(self.env.company.openmes_order_source, "manufacturing_orders")
        self.assertEqual(self.env["openmes.client"]._order_source(), "manufacturing_orders")

    def test_invalid_values_are_rejected(self):
        invalid = [
            {"openmes_api_url": "ftp://mes.example.test"},
            {"openmes_api_url": "mes.example.test"},
            {"openmes_api_url": "https://user:pass@mes.example.test"},
            {"openmes_api_url": "https://mes.example.test/?x=1"},
            {"openmes_api_key": "not-an-openmes-key"},
            {"openmes_api_key": "omk_with space"},
            {"openmes_order_prefix": "ODOO/2026"},
            {"openmes_order_prefix": "X" * 21},
            {"openmes_api_key": False},
        ]
        for vals in invalid:
            with self.subTest(vals=vals), self.assertRaises(ValidationError):
                self._settings(**vals)

    # ── Default line ─────────────────────────────────────────────────────────

    def test_default_line_setting(self):
        self.assertEqual(self.env["res.config.settings"].create({}).openmes_default_line_id, self.line_l1)
        self.env.company.openmes_default_line_id = self.line_l2
        settings = self.env["res.config.settings"].create({})
        self.assertEqual(settings.openmes_default_line_id, self.line_l2)
        self.assertEqual(settings.openmes_default_line_code, "L2")

        self._settings(openmes_default_line_id=self.line_l1.id).execute()
        self.assertEqual(self.env.company.openmes_default_line_id, self.line_l1)
        self._settings(openmes_default_line_id=False).execute()
        self.assertFalse(self.env.company.openmes_default_line_id)

    def test_default_line_cannot_be_deleted(self):
        with self.assertRaises(UserError):
            self.line_l1.unlink()
        spare = self.env["openmes.line"].create({"code": "L9", "name": "Spare"})
        spare.unlink()

    # ── Test Connection ──────────────────────────────────────────────────────

    @staticmethod
    def _me(**data):
        info = {
            "name": "Odoo",
            "scopes": list(ALL_SCOPES),
            "missing_scopes": [],
            "tenant": {"id": 1, "name": "Main plant"},
            "expires_at": None,
            "modules": {"erp": True, "warehouse": True},
            "app_version": "v0.27.1",
        }
        info.update(data)
        return FakeResponse(200, {"data": info})

    def _test_connection(self, *responses):
        settings = self._settings()
        with self.mock_openmes(*responses) as calls:
            result = settings.action_openmes_test_connection()
        return result["params"], calls

    def test_test_connection_all_scopes(self):
        params, calls = self._test_connection(self._me(expires_at="2027-01-31T23:59:00+00:00"))
        self.assertEqual(params["type"], "success")
        self.assertIn("Odoo", params["message"])
        self.assertIn("Main plant", params["message"])
        self.assertIn("2027-01-31", params["message"])
        self.assertIn("Warehouses module: enabled", params["message"])
        self.assertNotIn("Missing", params["message"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["url"], f"{API_URL}/api/v1/erp/me")

    def test_test_connection_missing_scopes(self):
        granted = [s for s in ALL_SCOPES if s not in ("erp:stock:read", "erp:webhooks:manage")]
        params, _calls = self._test_connection(self._me(scopes=granted + ["erp:quality:read"]))
        self.assertEqual(params["type"], "warning")
        message = params["message"]
        self.assertIn("erp:stock:read: stock documents and stock snapshot", message)
        self.assertIn("erp:webhooks:manage: registering webhooks", message)
        for scope in granted:
            self.assertNotIn(scope + ":", message)

        # Only missing_scopes given: scopes the module does not use are not listed.
        params, _calls = self._test_connection(
            self._me(scopes=None, missing_scopes=["erp:orders:import", "erp:quality:read", 7])
        )
        self.assertEqual(params["type"], "warning")
        self.assertIn("erp:orders:import", params["message"])
        self.assertNotIn("erp:quality:read", params["message"])

        params, _calls = self._test_connection(self._me(scopes=None, missing_scopes=None))
        self.assertEqual(params["type"], "warning")
        self.assertIn("did not report the key's scopes", params["message"])

    def test_test_connection_warehouses_module(self):
        params, _calls = self._test_connection(self._me(modules={"erp": True, "warehouse": False}))
        self.assertEqual(params["type"], "success", "stock sync is not used: information only")
        self.assertIn("Warehouses module is disabled", params["message"])

        self.env["stock.warehouse"].search([], limit=1).openmes_code = "WH-MAIN"
        params, _calls = self._test_connection(self._me(modules={"erp": True, "warehouse": False}))
        self.assertEqual(params["type"], "warning")

    def test_test_connection_old_openmes(self):
        params, calls = self._test_connection(
            FakeResponse(404, {"message": "Not Found"}),
            FakeResponse(200, {"data": [], "meta": {"next_cursor": None}}),
        )
        self.assertEqual(params["type"], "warning")
        self.assertIn("cannot report", params["message"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["url"].endswith("/api/v1/erp/production/completions"))
        self.assertEqual(calls[1]["params"], {"limit": 1})

        params, calls = self._test_connection(
            FakeResponse(404, {"message": "Not Found"}), FakeResponse(404, {"message": "Not Found"})
        )
        self.assertEqual(params["type"], "danger")
        self.assertIn("ERP integration module", params["message"])

    def test_test_connection_errors(self):
        cases = [
            (FakeResponse(401, {"message": "Unauthenticated"}), "rejected the API key"),
            (FakeResponse(500, None), "HTTP 500"),
            (lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")), "Cannot reach OpenMES"),
        ]
        for response, text in cases:
            with self.subTest(text=text), mute_logger(CLIENT_LOGGER):
                params, calls = self._test_connection(response)
                self.assertEqual(params["type"], "danger")
                self.assertIn(text, params["message"])
                self.assertEqual(len(calls), 1, "no fallback call")

        params, _calls = self._test_connection(FakeResponse(200, {"data": ["not", "a", "dict"]}))
        self.assertEqual(params["type"], "warning")
        self.assertIn("did not describe", params["message"])

    def test_redirect_is_not_followed(self):
        settings = self._settings()
        with self.mock_openmes(FakeResponse(301, None)):
            result = settings.action_openmes_test_connection()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertIn("redirect", result["params"]["message"])

    def test_sync_buttons_require_admin(self):
        settings = self._settings()
        manager = new_test_user(self.env, "om_sale_manager", groups="sales_team.group_sale_manager")
        for method in ("action_openmes_test_connection", "action_openmes_refresh_lines",
                       "action_openmes_sync_master_data", "action_openmes_sync_stock",
                       "action_openmes_sync_stock_documents", "action_openmes_register_webhook",
                       "action_openmes_unregister_webhook"):
            with self.subTest(method=method), self.assertRaises(AccessError):
                getattr(settings.with_user(manager), method)()

    def test_api_key_never_logged(self):
        settings = self._settings()
        with self.assertLogs("odoo.addons.openmes_connector.models.openmes_client", level="WARNING") as logs, \
                self.mock_openmes(FakeResponse(500, None)):
            settings.action_openmes_test_connection()
        self.assertFalse(any(API_KEY in line for line in logs.output))

        with self.assertLogs("odoo.addons.openmes_connector.models.openmes_client", level="WARNING") as logs, \
                self.mock_openmes(FakeResponse(404, None), FakeResponse(500, None)):
            settings.action_openmes_test_connection()
        self.assertTrue(logs.output)
        self.assertFalse(any(API_KEY in line for line in logs.output))
