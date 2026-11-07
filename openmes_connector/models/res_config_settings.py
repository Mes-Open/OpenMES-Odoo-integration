import logging
from urllib.parse import urlparse

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .openmes_client import (
    REQUIRED_SCOPES,
    WEBHOOK_EVENTS,
    WEBHOOK_PATH,
    OpenmesApiError,
    parse_iso_datetime,
)

_logger = logging.getLogger(__name__)

# Settings fields stored on the company (field name on both models).
OPENMES_COMPANY_FIELDS = (
    "openmes_enabled",
    "openmes_api_url",
    "openmes_api_key",
    "openmes_default_line_id",
    "openmes_order_prefix",
    "openmes_order_source",
)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # Every setting belongs to the company of the Settings form (the current
    # company): each company talks to its own OpenMES instance.
    openmes_enabled = fields.Boolean(
        "OpenMES Integration", related="company_id.openmes_enabled", readonly=False, groups="base.group_system"
    )
    openmes_api_url = fields.Char(
        "OpenMES URL", related="company_id.openmes_api_url", readonly=False, groups="base.group_system"
    )
    openmes_api_key = fields.Char(
        "OpenMES API Key", related="company_id.openmes_api_key", readonly=False, groups="base.group_system"
    )
    openmes_default_line_id = fields.Many2one(
        "openmes.line",
        "Default Line",
        related="company_id.openmes_default_line_id",
        readonly=False,
        groups="base.group_system",
        domain="[('company_id', '=', company_id)]",
        help="OpenMES line used when a product has no line of its own.",
    )
    openmes_default_line_code = fields.Char(
        "Default Line Code", related="openmes_default_line_id.code", readonly=True, groups="base.group_system"
    )
    openmes_order_prefix = fields.Char(
        "Work Order Prefix", related="company_id.openmes_order_prefix", readonly=False, groups="base.group_system"
    )
    openmes_order_source = fields.Selection(
        string="Create OpenMES work orders from",
        related="company_id.openmes_order_source",
        readonly=False,
        groups="base.group_system",
    )
    openmes_webhook_registered = fields.Boolean("Webhook Registered", compute="_compute_openmes_webhook")
    openmes_webhook_id = fields.Integer("OpenMES Webhook ID", compute="_compute_openmes_webhook")
    openmes_webhook_url = fields.Char(
        "Webhook URL",
        compute="_compute_openmes_webhook",
        help="The address OpenMES sends events to: the registered one, or web.base.url + /openmes/webhook.",
    )
    openmes_webhook_insecure = fields.Boolean("Webhook URL Not HTTPS", compute="_compute_openmes_webhook")
    openmes_webhook_incomplete = fields.Boolean(
        "Webhook Incomplete",
        compute="_compute_openmes_webhook",
        help="Odoo knows a webhook id but not its signing secret: unregister it, then register again.",
    )

    # Without these dependencies the settings form (an onchange on a new
    # record) never recomputed the status for its company: it always showed
    # "Not registered" and hid "Unregister Webhook".
    @api.depends(
        "company_id",
        "company_id.openmes_webhook_id",
        "company_id.openmes_webhook_secret",
        "company_id.openmes_webhook_url",
    )
    def _compute_openmes_webhook(self):
        target = self._openmes_webhook_target_url()
        for settings in self:
            company = settings.company_id.sudo()
            webhook_id = company.openmes_webhook_id or 0
            registered = bool(webhook_id and company.openmes_webhook_secret)
            url = (registered and company.openmes_webhook_url) or target
            settings.openmes_webhook_registered = registered
            settings.openmes_webhook_incomplete = bool(webhook_id) and not registered
            settings.openmes_webhook_id = webhook_id
            settings.openmes_webhook_url = url
            settings.openmes_webhook_insecure = urlparse(url).scheme != "https"

    def _openmes_webhook_target_url(self):
        base = (self.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").strip().rstrip("/")
        return base + WEBHOOK_PATH

    @api.model_create_multi
    def create(self, vals_list):
        """Write the OpenMES values of the form in ONE ``company.write``.

        Each related field would otherwise be written on its own, and the
        company constraint (enabled needs a URL and a key) would see the
        switch before the URL and key of the same save."""
        clean, pending = [], []
        for vals in vals_list:
            vals = dict(vals)
            pending.append({name: vals.pop(name) for name in list(vals) if name in OPENMES_COMPANY_FIELDS})
            clean.append(vals)
        records = super().create(clean)
        for settings, company_vals in zip(records, pending):
            changed = {}
            for name, value in company_vals.items():
                field = self._fields[name]
                new = field.convert_to_write(field.convert_to_record(field.convert_to_cache(value, settings), settings),
                                             settings)
                old = field.convert_to_write(settings[name], settings)
                if (new or False) != (old or False):
                    changed[name] = new
            if changed:
                # As the user: company field groups still apply.
                settings.company_id.write(changed)
                settings.invalidate_recordset(list(changed))
        return records

    def _openmes_company(self):
        """The company of the form, as superuser (the buttons are admin-only)."""
        self.ensure_one()
        return self.company_id.sudo()

    def _openmes_client(self):
        return self.env["openmes.client"].sudo().with_company(self._openmes_company())

    # ── Buttons ──────────────────────────────────────────────────────────────

    def _openmes_check_admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators can run OpenMES synchronisation."))

    def _openmes_notify(self, title, message, kind):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": kind != "success"},
        }

    def _openmes_log_summary(self, log):
        operation = dict(log._fields["operation"].selection)[log.operation]
        if log.state == "failed" and log.message and not log.sent_count:
            return f"{operation}: {log.message}"
        if log.operation == "lines":
            return _(
                "%(operation)s: %(fetched)s fetched, %(created)s created, %(updated)s updated, "
                "%(deactivated)s deactivated, %(errors)s errors",
                operation=operation,
                fetched=log.sent_count,
                created=log.imported_count,
                updated=log.updated_count,
                deactivated=log.skipped_count,
                errors=log.error_count,
            )
        if log.operation == "stock_documents":
            return _(
                "%(operation)s: %(fetched)s fetched, %(booked)s booked, %(acked)s acknowledged, %(errors)s errors",
                operation=operation,
                fetched=log.sent_count,
                booked=log.imported_count,
                acked=log.updated_count,
                errors=log.error_count,
            )
        return _(
            "%(operation)s: %(created)s created, %(updated)s updated, %(skipped)s skipped, %(errors)s errors",
            operation=operation,
            created=log.imported_count,
            updated=log.updated_count,
            skipped=log.skipped_count,
            errors=log.error_count,
        )

    def _openmes_notify_logs(self, title, logs):
        if not logs:
            return self._openmes_notify(title, _("Nothing to synchronise."), "info")
        summary = "\n".join(self._openmes_log_summary(log) for log in logs)
        kind = "success" if all(log.state == "success" for log in logs) else "warning"
        return self._openmes_notify(title, summary, kind)

    def _openmes_scope_features(self):
        return {
            "erp:orders:import": _("sending and cancelling work orders"),
            "erp:production:read": _("status pull, change requests and settlement"),
            "erp:masterdata:read": _("production line list"),
            "erp:masterdata:write": _("products, materials, material lots, BoMs and routings"),
            "erp:stock:read": _("stock documents and stock snapshot"),
            "erp:stock:write": _("stock snapshot and acknowledging stock documents"),
            "erp:webhooks:manage": _("registering webhooks"),
        }

    @api.model
    def _openmes_missing_scopes(self, info):
        """Required scopes the key lacks, or None when OpenMES did not say."""
        scopes = info.get("scopes")
        if isinstance(scopes, list):
            granted = {scope for scope in scopes if isinstance(scope, str)}
            return [scope for scope in REQUIRED_SCOPES if scope not in granted]
        missing = info.get("missing_scopes")
        if isinstance(missing, list):
            missing = {scope for scope in missing if isinstance(scope, str)}
            return [scope for scope in REQUIRED_SCOPES if scope in missing]
        return None

    def _openmes_key_report(self, info):
        """``(message, ok)`` describing what the key of ``GET /me`` may do."""
        ok = True
        name = info.get("name") if isinstance(info.get("name"), str) else "?"
        tenant = info.get("tenant") if isinstance(info.get("tenant"), dict) else {}
        version = info.get("app_version") if isinstance(info.get("app_version"), str) else "?"
        lines = []
        if tenant.get("name"):
            lines.append(_(
                "Connected as key “%(name)s” (tenant %(tenant)s, OpenMES %(version)s).",
                name=name, tenant=tenant["name"], version=version,
            ))
        else:
            lines.append(_("Connected as key “%(name)s” (OpenMES %(version)s).", name=name, version=version))
        expires = parse_iso_datetime(info.get("expires_at"))
        if expires:
            lines.append(_("The key expires on %s.", fields.Datetime.to_string(expires)))

        missing = self._openmes_missing_scopes(info)
        if missing is None:
            ok = False
            lines.append(_("OpenMES did not report the key's scopes."))
        elif missing:
            ok = False
            features = self._openmes_scope_features()
            lines.append(_("Missing scopes (these features will fail):"))
            lines.extend(f"- {scope}: {features[scope]}" for scope in missing)
        else:
            lines.append(_("The key has every scope the module uses."))

        modules = info.get("modules") if isinstance(info.get("modules"), dict) else {}
        if modules.get("warehouse") is True:
            lines.append(_("Warehouses module: enabled."))
        else:
            lines.append(_(
                "Warehouses module is disabled in OpenMES: stock snapshots and stock documents are unavailable."
            ))
            if self.env["stock.warehouse"].sudo().search_count(
                [("openmes_code", "!=", False), ("company_id", "=", self.company_id.id)], limit=1
            ):
                ok = False
        return "\n".join(lines), ok

    def action_openmes_test_connection(self):
        self._openmes_check_admin()
        self.ensure_one()
        client = self._openmes_client()
        url, key = self.openmes_api_url or "", self.openmes_api_key or ""
        try:
            info = client._get_key_info(url, key)
            if info is None:
                # OpenMES before GET /erp/me: check the ERP API the old way.
                client._test_connection(url, key)
        except OpenmesApiError as exc:
            return self._openmes_notify(_("OpenMES connection failed"), str(exc), "danger")
        if info is None:
            return self._openmes_notify(_("OpenMES"), _(
                "Connected. This OpenMES version cannot report the key's scopes (GET /api/v1/erp/me); "
                "update OpenMES to check them."
            ), "warning")
        if not isinstance(info, dict):
            return self._openmes_notify(
                _("OpenMES"), _("Connected, but OpenMES did not describe the API key."), "warning"
            )
        message, ok = self._openmes_key_report(info)
        return self._openmes_notify(_("OpenMES"), message, "success" if ok else "warning")

    def action_openmes_refresh_lines(self):
        self._openmes_check_admin()
        log = self.env["openmes.line"].sudo().with_company(self._openmes_company())._openmes_refresh()
        return self._openmes_notify_logs(_("OpenMES lines"), log)

    def action_openmes_sync_master_data(self):
        self._openmes_check_admin()
        logs = self.env["openmes.sync"].with_company(self._openmes_company())._sync_master_data()
        return self._openmes_notify_logs(_("OpenMES master data"), logs)

    def action_openmes_sync_stock(self):
        self._openmes_check_admin()
        logs = self.env["openmes.sync"].with_company(self._openmes_company())._sync_all_stock()
        if not logs.filtered(lambda log: log.operation == "stock"):
            return self._openmes_notify(
                _("OpenMES stock"), _("No warehouse has an OpenMES warehouse code."), "info"
            )
        return self._openmes_notify_logs(_("OpenMES stock"), logs)

    def action_openmes_sync_stock_documents(self):
        self._openmes_check_admin()
        log, _read = self.env["openmes.stock.document"].with_company(
            self._openmes_company()
        )._openmes_sync_documents()
        return self._openmes_notify_logs(_("OpenMES stock documents"), log)

    # ── Webhooks ─────────────────────────────────────────────────────────────

    def _openmes_notify_reload(self, title, message, kind):
        """A notification that reloads the settings form, so the webhook status
        shown matches what was stored (or not) by the button."""
        action = self._openmes_notify(title, message, kind)
        action["params"]["next"] = {"type": "ir.actions.client", "tag": "soft_reload"}
        return action

    def _openmes_clear_webhook(self):
        self._openmes_company().write(
            {"openmes_webhook_id": 0, "openmes_webhook_secret": False, "openmes_webhook_url": False}
        )

    def action_openmes_register_webhook(self):
        self._openmes_check_admin()
        self.ensure_one()
        title = _("OpenMES webhook")
        if self.openmes_webhook_registered:
            return self._openmes_notify_reload(title, _("A webhook is already registered."), "info")
        if self.openmes_webhook_incomplete:
            return self._openmes_notify_reload(
                title,
                _("Odoo has no signing secret for webhook %s: unregister it first, then register again.",
                  self.openmes_webhook_id),
                "info",
            )
        url = self._openmes_webhook_target_url()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return self._openmes_notify_reload(
                title, _("Set web.base.url to the address OpenMES can reach Odoo at."), "danger"
            )
        client = self._openmes_client()
        name = f"Odoo ({self.env.cr.dbname}, {self.company_id.name})"
        try:
            answer = client._register_webhook(name, url, WEBHOOK_EVENTS)
        except OpenmesApiError as exc:
            return self._openmes_notify_reload(_("OpenMES webhook registration failed"), str(exc), "danger")

        data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
        secret = answer.get("secret")
        webhook_id = data.get("id")
        if not isinstance(webhook_id, int) or isinstance(webhook_id, bool) or webhook_id <= 0 \
                or not isinstance(secret, str) or len(secret) < 16:
            if isinstance(webhook_id, int) and not isinstance(webhook_id, bool) and webhook_id > 0:
                # Without its secret the webhook is useless: remove it again.
                try:
                    client._delete_webhook(webhook_id)
                except OpenmesApiError as exc:
                    _logger.warning("Removing OpenMES webhook %s without a secret failed: %s", webhook_id, exc)
            return self._openmes_notify_reload(
                _("OpenMES webhook registration failed"),
                _("OpenMES did not return the webhook id and signing secret."),
                "danger",
            )

        self._openmes_company().write(
            {"openmes_webhook_id": webhook_id, "openmes_webhook_secret": secret, "openmes_webhook_url": url}
        )
        if parsed.scheme != "https":
            return self._openmes_notify_reload(
                title,
                _(
                    "Webhook registered (id %(id)s) for %(url)s. The address is not https: events "
                    "travel unencrypted. Use https in production.",
                    id=webhook_id,
                    url=url,
                ),
                "warning",
            )
        return self._openmes_notify_reload(
            title, _("Webhook registered (id %(id)s) for %(url)s.", id=webhook_id, url=url), "success"
        )

    def action_openmes_unregister_webhook(self):
        self._openmes_check_admin()
        self.ensure_one()
        title = _("OpenMES webhook")
        webhook_id = self._openmes_company().openmes_webhook_id
        if not webhook_id:
            self._openmes_clear_webhook()
            return self._openmes_notify_reload(title, _("No webhook is registered."), "info")
        if webhook_id > 0:
            try:
                self._openmes_client()._delete_webhook(webhook_id)
            except OpenmesApiError as exc:
                # 404: already deleted in OpenMES (or by its administrator).
                if exc.status_code != 404:
                    return self._openmes_notify_reload(_("OpenMES webhook removal failed"), str(exc), "danger")
        self._openmes_clear_webhook()
        return self._openmes_notify_reload(
            title, _("Webhook unregistered. Odoo polls OpenMES every 15 minutes again."), "success"
        )
