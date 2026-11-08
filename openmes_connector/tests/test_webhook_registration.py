import requests

from odoo.exceptions import AccessError
from odoo.tests import Form, tagged
from odoo.tests.common import new_test_user

from .common import API_URL, FakeResponse, OpenmesCase

# Odoo 20: ir.config_parameter.get_param/set_param are gone; the typed
# get_str/set_str replace them (same stored text, same False default).
SECRET = "Zx8" + "s" * 37
EVENTS = [
    "work_order.created",
    "work_order.status_changed",
    "work_order.progress",
    "stock_document.posted",
    "quality_issue.created",
]


@tagged("post_install", "-at_install")
class TestWebhookRegistration(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.icp = cls.env["ir.config_parameter"].sudo()
        cls.icp.set_str("web.base.url", "https://odoo.example.test")
        cls.cron = cls.env.ref("openmes_connector.ir_cron_openmes_pull_status")
        cls.cron.write({"interval_number": 15, "interval_type": "minutes"})

    def _settings(self):
        return self.env["res.config.settings"].create({})

    def _stored(self, company=None):
        company = (company or self.env.company).sudo()
        return (
            str(company.openmes_webhook_id) if company.openmes_webhook_id else False,
            company.openmes_webhook_secret or False,
            company.openmes_webhook_url or False,
        )

    def _interval(self):
        return (self.cron.interval_number, self.cron.interval_type)

    def _register_ok(self, settings=None):
        settings = settings or self._settings()
        with self.mock_openmes(FakeResponse(201, {"data": {"id": 12, "is_active": True}, "secret": SECRET})) as calls:
            result = settings.action_openmes_register_webhook()
        return result, calls

    # ── Register ─────────────────────────────────────────────────────────────

    def test_register_stores_secret_and_slows_the_pull(self):
        self.assertFalse(self._settings().openmes_webhook_registered)
        self.assertEqual(self._settings().openmes_webhook_url, "https://odoo.example.test/openmes/webhook")
        result, calls = self._register_ok()
        self.assertEqual(result["params"]["type"], "success")
        call = calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", f"{API_URL}/api/v1/erp/webhooks"))
        self.assertEqual(call["json"]["url"], "https://odoo.example.test/openmes/webhook")
        self.assertEqual(call["json"]["events"], EVENTS)
        self.assertIs(call["json"]["is_active"], True)
        self.assertNotIn("secret", call["json"])
        self.assertTrue(call["json"]["name"].startswith("Odoo ("))
        self.assertEqual(self._stored(), ("12", SECRET, "https://odoo.example.test/openmes/webhook"))
        self.assertEqual(self._interval(), (1, "hours"))
        settings = self._settings()
        self.assertTrue(settings.openmes_webhook_registered)
        self.assertEqual(settings.openmes_webhook_id, 12)
        self.assertFalse(settings.openmes_webhook_insecure)

    def test_registered_url_stays_shown_when_base_url_changes(self):
        self._register_ok()
        self.icp.set_str("web.base.url", "http://localhost:8069")
        self.assertEqual(self._settings().openmes_webhook_url, "https://odoo.example.test/openmes/webhook")

    def test_http_base_url_is_warned_about(self):
        self.icp.set_str("web.base.url", "http://192.168.1.50:8069")
        self.assertTrue(self._settings().openmes_webhook_insecure)
        result, calls = self._register_ok()
        self.assertEqual(calls[0]["json"]["url"], "http://192.168.1.50:8069/openmes/webhook")
        self.assertEqual(result["params"]["type"], "warning")
        self.assertIn("https", result["params"]["message"])
        self.assertTrue(self._settings().openmes_webhook_insecure)

    def test_register_errors_store_nothing(self):
        def timeout(method, url, **kwargs):
            raise requests.Timeout("timed out")

        answers = {
            "no scope": FakeResponse(403, {"message": "Forbidden"}),
            "private address": FakeResponse(422, {
                "message": "invalid", "errors": {"url": ["Webhook URL resolves to a private/reserved address."]}}),
            "server error": FakeResponse(500, None),
            "timeout": timeout,
        }
        for name, answer in answers.items():
            with self.subTest(case=name):
                with self.mock_openmes(answer):
                    result = self._settings().action_openmes_register_webhook()
                self.assertEqual(result["params"]["type"], "danger")
                self.assertEqual(self._stored(), (False, False, False))
                self.assertEqual(self._interval(), (15, "minutes"))
        with self.mock_openmes(answers["private address"]):
            result = self._settings().action_openmes_register_webhook()
        self.assertIn("private/reserved", result["params"]["message"])

    def test_answer_without_secret_removes_the_webhook_again(self):
        with self.mock_openmes(FakeResponse(201, {"data": {"id": 12}}), FakeResponse(204, None)) as calls:
            result = self._settings().action_openmes_register_webhook()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertEqual((calls[1]["method"], calls[1]["url"]), ("DELETE", f"{API_URL}/api/v1/erp/webhooks/12"))
        self.assertEqual(self._stored(), (False, False, False))
        self.assertEqual(self._interval(), (15, "minutes"))

    def test_already_registered_makes_no_call(self):
        self._register_ok()
        with self.mock_openmes() as calls:
            result = self._settings().action_openmes_register_webhook()
        self.assertFalse(calls)
        self.assertEqual(result["params"]["type"], "info")

    # ── Unregister ───────────────────────────────────────────────────────────

    def test_unregister_clears_state_and_restores_the_pull(self):
        for status in (204, 404):
            with self.subTest(status=status):
                self._register_ok()
                with self.mock_openmes(FakeResponse(status, None)) as calls:
                    result = self._settings().action_openmes_unregister_webhook()
                self.assertEqual((calls[0]["method"], calls[0]["url"]),
                                 ("DELETE", f"{API_URL}/api/v1/erp/webhooks/12"))
                self.assertEqual(result["params"]["type"], "success")
                self.assertEqual(self._stored(), (False, False, False))
                self.assertEqual(self._interval(), (15, "minutes"))
                self.assertFalse(self._settings().openmes_webhook_registered)

    def test_unregister_error_keeps_state(self):
        self._register_ok()
        with self.mock_openmes(FakeResponse(500, None)):
            result = self._settings().action_openmes_unregister_webhook()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertEqual(self._stored()[0], "12")
        self.assertEqual(self._interval(), (1, "hours"))

    # ── Access and secrecy ───────────────────────────────────────────────────

    def test_only_admins_manage_webhooks(self):
        settings = self._settings()
        users = [
            new_test_user(self.env, "om_wh_sale_manager", groups="sales_team.group_sale_manager"),
            new_test_user(self.env, "om_wh_salesman", groups="sales_team.group_sale_salesman"),
            new_test_user(self.env, "om_wh_mrp_manager", groups="mrp.group_mrp_manager"),
        ]
        for user in users:
            for method in ("action_openmes_register_webhook", "action_openmes_unregister_webhook"):
                with self.subTest(user=user.login, method=method), self.mock_openmes(), \
                        self.assertRaises(AccessError):
                    getattr(settings.with_user(user), method)()
        self.assertEqual(self._stored(), (False, False, False))
        # The event log: managers read it, a salesman does not.
        self.env["openmes.webhook.event"].with_user(users[0]).search([])
        with self.assertRaises(AccessError):
            self.env["openmes.webhook.event"].with_user(users[1]).search([])

    def test_secret_never_logged(self):
        self._register_ok()
        with self.assertLogs("odoo.addons.openmes_connector", level="WARNING") as logs, \
                self.mock_openmes(FakeResponse(500, None)):
            self._settings().action_openmes_unregister_webhook()
        self.assertFalse(any(SECRET in line for line in logs.output))


RELOAD = {"type": "ir.actions.client", "tag": "soft_reload"}
WEBHOOK_URL = "https://odoo.example.test/openmes/webhook"


@tagged("post_install", "-at_install")
class TestWebhookSettingsStatus(OpenmesCase):
    """The settings form shows the webhook of ITS company (it is an onchange on
    a new record, so the status must be recomputed from ``company_id``)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_str("web.base.url", "https://odoo.example.test")
        cls.company_b = cls.env["res.company"].create({"name": "OpenMES Company B"})
        cls.env.user.company_ids |= cls.company_b
        cls.Settings = cls.env["res.config.settings"]

    def _set_webhook(self, company=None, webhook_id=12, secret=SECRET, url=WEBHOOK_URL):
        self._configure(company, webhook_id=webhook_id, webhook_secret=secret, webhook_url=url)

    def _status(self, settings):
        return (
            settings.openmes_webhook_registered,
            settings.openmes_webhook_incomplete,
            settings.openmes_webhook_id,
            settings.openmes_webhook_url,
        )

    def test_form_shows_registered_webhook(self):
        self._set_webhook(url="https://odoo-old.example.test/openmes/webhook")
        form = Form(self.Settings)
        self.assertEqual(self._status(form), (True, False, 12, "https://odoo-old.example.test/openmes/webhook"))
        self.assertFalse(form.openmes_webhook_insecure)

    def test_form_shows_not_registered_without_webhook(self):
        form = Form(self.Settings)
        self.assertEqual(self._status(form), (False, False, 0, WEBHOOK_URL))

    def test_form_follows_company_switch(self):
        self._set_webhook()
        self.assertEqual(self._status(Form(self.Settings.with_company(self.company_b)))[:3], (False, False, 0))
        self.assertEqual(self._status(Form(self.Settings.with_company(self.env.company)))[:3], (True, False, 12))
        # Changing the company of an open form recomputes the status.
        settings = self.Settings.new({"company_id": self.env.company.id})
        self.assertTrue(settings.openmes_webhook_registered)
        settings.company_id = self.company_b
        self.assertEqual(self._status(settings), (False, False, 0, WEBHOOK_URL))
        settings.company_id = self.env.company
        self.assertEqual(self._status(settings)[:3], (True, False, 12))

    def test_reloaded_form_shows_the_new_state(self):
        # Odoo does not recompute fields of transient records when a regular
        # model changes (fields.py), so an open settings form keeps its value:
        # the webhook buttons reload the form, which builds a new record.
        form = Form(self.Settings)
        self.assertFalse(form.openmes_webhook_registered)
        with self.mock_openmes(FakeResponse(201, {"data": {"id": 12}, "secret": SECRET})):
            result = form.save().action_openmes_register_webhook()
        self.assertEqual(result["params"]["next"], RELOAD)
        self.assertEqual(self._status(Form(self.Settings)), (True, False, 12, WEBHOOK_URL))
        with self.mock_openmes(FakeResponse(204, None)):
            result = Form(self.Settings).save().action_openmes_unregister_webhook()
        self.assertEqual(result["params"]["next"], RELOAD)
        self.assertEqual(self._status(Form(self.Settings)), (False, False, 0, WEBHOOK_URL))

    def test_id_without_secret_is_incomplete(self):
        self._set_webhook(secret=False)
        form = Form(self.Settings)
        self.assertEqual(self._status(form)[:3], (False, True, 12))
        settings = self.Settings.create({})
        with self.mock_openmes() as calls:
            result = settings.action_openmes_register_webhook()
        self.assertFalse(calls)
        self.assertEqual(result["params"]["type"], "info")
        self.assertIn("12", result["params"]["message"])
        with self.mock_openmes(FakeResponse(204, None)) as calls:
            result = settings.action_openmes_unregister_webhook()
        self.assertEqual((calls[0]["method"], calls[0]["url"]), ("DELETE", f"{API_URL}/api/v1/erp/webhooks/12"))
        self.assertEqual(result["params"]["type"], "success")
        company = self.env.company.sudo()
        self.assertEqual((company.openmes_webhook_id, company.openmes_webhook_secret), (0, False))
        self.assertEqual(self._status(self.Settings.create({}))[:3], (False, False, 0))

    # ── Reload after the webhook buttons ─────────────────────────────────────

    def test_webhook_actions_reload_settings(self):
        def timeout(method, url, **kwargs):
            raise requests.Timeout("timed out")

        ok = FakeResponse(201, {"data": {"id": 12}, "secret": SECRET})
        icp = self.env["ir.config_parameter"].sudo()
        cases = [
            # (name, state before, base url, method, responses)
            ("register success", None, "https://odoo.example.test", "register", [ok]),
            ("register http warning", None, "http://odoo.example.test", "register", [ok]),
            ("already registered", (12, SECRET), "https://odoo.example.test", "register", []),
            ("incomplete", (12, False), "https://odoo.example.test", "register", []),
            ("register 403", None, "https://odoo.example.test", "register", [FakeResponse(403, {"message": "x"})]),
            ("register 422", None, "https://odoo.example.test", "register", [FakeResponse(422, {"message": "x"})]),
            ("register 500", None, "https://odoo.example.test", "register", [FakeResponse(500, None)]),
            ("register timeout", None, "https://odoo.example.test", "register", [timeout]),
            ("no secret", None, "https://odoo.example.test", "register",
             [FakeResponse(201, {"data": {"id": 12}}), FakeResponse(204, None)]),
            ("bad base url", None, "", "register", []),
            ("unregister 204", (12, SECRET), "https://odoo.example.test", "unregister", [FakeResponse(204, None)]),
            ("unregister 404", (12, SECRET), "https://odoo.example.test", "unregister", [FakeResponse(404, None)]),
            ("unregister 500", (12, SECRET), "https://odoo.example.test", "unregister", [FakeResponse(500, None)]),
            ("unregister nothing", None, "https://odoo.example.test", "unregister", []),
        ]
        for name, state, base, method, responses in cases:
            with self.subTest(case=name):
                webhook_id, secret = state or (0, False)
                self._set_webhook(webhook_id=webhook_id, secret=secret, url=WEBHOOK_URL if state else False)
                icp.set_str("web.base.url", base)
                settings = self.Settings.create({})
                with self.mock_openmes(*responses):
                    result = getattr(settings, f"action_openmes_{method}_webhook")()
                self.assertEqual(result["tag"], "display_notification")
                self.assertEqual(result["params"]["next"], RELOAD)

    def test_other_settings_buttons_do_not_reload(self):
        me = {"name": "odoo", "scopes": [], "modules": {"warehouse": True}}
        with self.mock_openmes(FakeResponse(200, {"data": me})):
            result = self.Settings.create({}).action_openmes_test_connection()
        self.assertNotIn("next", result["params"])

    def test_webhook_fields_hidden_from_non_admins(self):
        self._set_webhook()
        salesman = new_test_user(self.env, "om_wh_status_salesman", groups="sales_team.group_sale_salesman")
        company = self.env.company.with_user(salesman)
        for field in ("openmes_webhook_id", "openmes_webhook_secret", "openmes_webhook_url"):
            with self.subTest(field=field), self.assertRaises(AccessError):
                company[field]
