"""OpenMES settings per company.

One company is one OpenMES instance (or tenant): its URL, API key, default
line, order prefix, order source, webhook and the ``since`` markers of the
status and change-request pulls live on the company. Settings shows the
fields of the current company through related fields.
"""

import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .openmes_client import (
    API_KEY_PREFIX,
    DEFAULT_ORDER_PREFIX,
    PARAM_API_KEY,
    PARAM_API_URL,
    PARAM_CR_SINCE,
    PARAM_DEFAULT_LINE,
    PARAM_ENABLED,
    PARAM_ORDER_PREFIX,
    PARAM_ORDER_SOURCE,
    PARAM_STATUS_SINCE,
    PARAM_WEBHOOK_ID,
    PARAM_WEBHOOK_SECRET,
    PARAM_WEBHOOK_URL,
    SOURCE_MANUFACTURING,
    SOURCE_SALE_LINES,
    parse_iso_datetime,
    validate_api_url,
)

_logger = logging.getLogger(__name__)

ORDER_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
# Status pull interval without and with a registered webhook (the pull stays as a fallback).
PULL_INTERVAL_POLLING = (15, "minutes")
PULL_INTERVAL_WEBHOOK = (1, "hours")
PULL_INTERVAL_FIELDS = {"openmes_enabled", "openmes_webhook_id", "openmes_webhook_secret"}
CONNECTION_FIELDS = {"openmes_api_url", "openmes_api_key"}
RESET_ON_RECONNECT = (
    "openmes_status_since",
    "openmes_cr_since",
    "openmes_webhook_id",
    "openmes_webhook_secret",
    "openmes_webhook_url",
)
LEGACY_PARAMS = (
    PARAM_ENABLED,
    PARAM_API_URL,
    PARAM_API_KEY,
    PARAM_DEFAULT_LINE,
    PARAM_ORDER_PREFIX,
    PARAM_ORDER_SOURCE,
    PARAM_STATUS_SINCE,
    PARAM_CR_SINCE,
    PARAM_WEBHOOK_ID,
    PARAM_WEBHOOK_SECRET,
    PARAM_WEBHOOK_URL,
)


class ResCompany(models.Model):
    _inherit = "res.company"

    # Every setting is admin-only: it changes what Odoo sends where. The code
    # reads them as superuser.
    openmes_enabled = fields.Boolean("OpenMES Integration", groups="base.group_system")
    openmes_api_url = fields.Char("OpenMES URL", groups="base.group_system", copy=False)
    openmes_api_key = fields.Char("OpenMES API Key", groups="base.group_system", copy=False)
    openmes_default_line_id = fields.Many2one(
        "openmes.line",
        "Default OpenMES Line",
        domain="[('company_id', '=', id)]",
        ondelete="restrict",
        copy=False,
        groups="base.group_system",
        help="OpenMES line used when a product has no line of its own in this company.",
    )
    openmes_order_prefix = fields.Char(
        "OpenMES Work Order Prefix", default=DEFAULT_ORDER_PREFIX, groups="base.group_system"
    )
    openmes_order_source = fields.Selection(
        [
            (SOURCE_SALE_LINES, "Sale order lines"),
            (SOURCE_MANUFACTURING, "Manufacturing orders"),
        ],
        string="Create OpenMES work orders from",
        default=SOURCE_SALE_LINES,
        groups="base.group_system",
    )
    openmes_webhook_id = fields.Integer("OpenMES Webhook ID", groups="base.group_system", copy=False)
    openmes_webhook_secret = fields.Char("OpenMES Webhook Secret", groups="base.group_system", copy=False)
    openmes_webhook_url = fields.Char("OpenMES Webhook URL", groups="base.group_system", copy=False)
    openmes_status_since = fields.Datetime(
        "OpenMES Status Pull Marker", groups="base.group_system", copy=False
    )
    openmes_cr_since = fields.Datetime(
        "OpenMES Change Request Pull Marker", groups="base.group_system", copy=False
    )

    # ── Normalisation and checks ─────────────────────────────────────────────

    @api.model
    def _openmes_normalize(self, vals):
        if vals.get("openmes_api_url"):
            vals["openmes_api_url"] = vals["openmes_api_url"].strip().rstrip("/")
        if vals.get("openmes_api_key"):
            vals["openmes_api_key"] = vals["openmes_api_key"].strip()
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create([self._openmes_normalize(dict(vals)) for vals in vals_list])
        if any(PULL_INTERVAL_FIELDS & set(vals) for vals in vals_list):
            self._openmes_update_pull_interval()
        return companies

    def write(self, vals):
        vals = self._openmes_normalize(dict(vals))
        before = {}
        if CONNECTION_FIELDS & set(vals):
            before = {
                company.id: (company.openmes_api_url or "", company.openmes_api_key or "")
                for company in self.sudo()
            }
        res = super().write(vals)
        reset = self._openmes_reset_after_reconnect(before, vals)
        # Wherever the change comes from (Settings, the company form, RPC, the
        # migration), the status pull interval follows it.
        if reset or PULL_INTERVAL_FIELDS & set(vals):
            self._openmes_update_pull_interval()
        return res

    def _openmes_reset_after_reconnect(self, before, vals):
        """Another URL or API key is another OpenMES instance (or a key whose
        webhooks OpenMES deactivated): its ``since`` markers and its webhook do
        not apply any more. Values written in the same call (the import of the
        global settings) are kept. Returns True when a company was reset."""
        reset = False
        for company in self.sudo().browse(list(before)):
            old_url, old_key = before[company.id]
            new_url, new_key = company.openmes_api_url or "", company.openmes_api_key or ""
            if not ((old_url and old_url != new_url) or (old_key and old_key != new_key)):
                continue
            clear = {field: False for field in RESET_ON_RECONNECT if field not in vals and company[field]}
            if not clear:
                continue
            if company.openmes_webhook_id and "openmes_webhook_id" in clear:
                # Only ids: never the secret or the key.
                _logger.warning(
                    "OpenMES connection of company %s changed: webhook %s of the former connection is "
                    "forgotten; delete it in that OpenMES (Settings > Webhooks) and register again.",
                    company.id,
                    company.openmes_webhook_id,
                )
            super(ResCompany, company).write(clear)
            reset = True
        return reset

    @api.constrains("openmes_enabled", "openmes_api_url", "openmes_api_key", "openmes_order_prefix")
    def _check_openmes_settings(self):
        for company in self.sudo():
            url = (company.openmes_api_url or "").strip()
            key = (company.openmes_api_key or "").strip()
            if company.openmes_enabled and (not url or not key):
                raise ValidationError(_("Enter the OpenMES URL and API key before enabling the integration."))
            if url:
                error = validate_api_url(self.env, url)
                if error:
                    raise ValidationError(error)
            if key and (not key.startswith(API_KEY_PREFIX) or re.search(r"\s", key) or len(key) > 255):
                raise ValidationError(
                    _("The OpenMES API key must start with “%s” (Settings → API Keys in OpenMES).", API_KEY_PREFIX)
                )
            prefix = company.openmes_order_prefix
            if prefix and not ORDER_PREFIX_RE.match(prefix):
                raise ValidationError(
                    _("The work order prefix may only contain letters, digits, “-” and “_” (max. 20 characters).")
                )
            if company.openmes_enabled and self.sudo().search_count([
                ("id", "!=", company.id),
                ("openmes_enabled", "=", True),
                ("openmes_api_url", "=", url),
                ("openmes_api_key", "=", key),
            ], limit=1):
                # Both companies would fetch, book and acknowledge the same
                # stock documents and status rows.
                raise ValidationError(_(
                    "Another company already uses this OpenMES URL and API key. Give each company "
                    "its own API key."
                ))

    # ── Helpers ──────────────────────────────────────────────────────────────

    @api.model
    def _openmes_enabled_companies(self):
        """Companies the crons work for, as superuser."""
        return self.sudo().search([("openmes_enabled", "=", True)], order="id")

    @api.model
    def _openmes_cron_each(self, label, callback, commit=False):
        """Run ``callback(company)`` for every enabled company, each one isolated:
        whatever one company's run raises (bad data, a bug) is logged and rolled
        back, and the other companies carry on.

        ``commit``: the run commits by itself (stock documents), so a savepoint
        cannot surround it; each company's work is committed instead, and a
        failure rolls back only the failing company's uncommitted work."""
        Document = self.env["openmes.stock.document"]
        for company in self._openmes_enabled_companies():
            try:
                if commit:
                    callback(company)
                    Document._openmes_commit()
                else:
                    with self.env.cr.savepoint():
                        callback(company)
            except Exception:
                _logger.exception("OpenMES %s failed for company %s", label, company.id)
                if commit:
                    Document._openmes_rollback()
                self.env.invalidate_all()

    @api.model
    def _openmes_update_pull_interval(self):
        """The status pull runs less often only when every enabled company is
        pushed to by a webhook; one company without a webhook keeps 15 minutes."""
        cron = self.env.ref("openmes_connector.ir_cron_openmes_pull_status", raise_if_not_found=False)
        if not cron:
            return
        companies = self._openmes_enabled_companies()
        pushed = companies and all(c.openmes_webhook_id and c.openmes_webhook_secret for c in companies)
        number, kind = PULL_INTERVAL_WEBHOOK if pushed else PULL_INTERVAL_POLLING
        if (cron.interval_number, cron.interval_type) != (number, kind):
            cron.sudo().write({"interval_number": number, "interval_type": kind})

    # ── Migration from the global ir.config_parameter settings ───────────────

    @api.model
    def _openmes_main_company(self):
        company = self.env.ref("base.main_company", raise_if_not_found=False)
        return (company or self.sudo().search([], order="id", limit=1)).sudo()

    @api.model
    def _openmes_adopt_legacy_params(self):
        """Move the global settings (up to 18.0.1.1.6) to the main company and
        delete the parameters. Idempotent; never overwrites a company that is
        configured already and never raises on bad legacy values."""
        icp = self.env["ir.config_parameter"].sudo()
        legacy = {key: icp.get_param(key) for key in LEGACY_PARAMS}
        if not any(legacy.values()):
            return False
        company = self._openmes_main_company()
        if company and not company.openmes_api_url:
            self._openmes_import_legacy(company, legacy)
        elif company:
            _logger.info("OpenMES settings of company %s are kept; the global ones are dropped.", company.id)
            webhook_id = legacy[PARAM_WEBHOOK_ID]
            if webhook_id and str(webhook_id) != str(company.openmes_webhook_id or ""):
                _logger.warning(
                    "OpenMES webhook %s registered with the former global settings is dropped: delete it in "
                    "OpenMES (Settings > Webhooks), or its deliveries are rejected with 401.",
                    webhook_id,
                )
        for key in LEGACY_PARAMS:
            icp.set_param(key, False)
        return True

    @api.model
    def _openmes_import_legacy(self, company, legacy):
        vals = {}
        if legacy[PARAM_API_URL]:
            vals["openmes_api_url"] = legacy[PARAM_API_URL]
        if legacy[PARAM_API_KEY]:
            vals["openmes_api_key"] = legacy[PARAM_API_KEY]
        if legacy[PARAM_ORDER_PREFIX]:
            vals["openmes_order_prefix"] = legacy[PARAM_ORDER_PREFIX]
        if legacy[PARAM_ORDER_SOURCE] in (SOURCE_SALE_LINES, SOURCE_MANUFACTURING):
            vals["openmes_order_source"] = legacy[PARAM_ORDER_SOURCE]
        webhook_id = legacy[PARAM_WEBHOOK_ID] or ""
        if webhook_id.isdigit() and legacy[PARAM_WEBHOOK_SECRET]:
            vals.update(
                openmes_webhook_id=int(webhook_id),
                openmes_webhook_secret=legacy[PARAM_WEBHOOK_SECRET],
                openmes_webhook_url=legacy[PARAM_WEBHOOK_URL] or False,
            )
        for param, field in ((PARAM_STATUS_SINCE, "openmes_status_since"), (PARAM_CR_SINCE, "openmes_cr_since")):
            since = parse_iso_datetime(legacy[param])
            if since:
                vals[field] = since
        code = (legacy[PARAM_DEFAULT_LINE] or "").strip()
        if code:
            Line = self.env["openmes.line"].sudo().with_context(active_test=False)
            line = Line.search([("code", "=", code), ("company_id", "=", company.id)], limit=1)
            if not line:
                line = Line._openmes_adopt_legacy_codes({}, code, company=company)[:1]
            if line:
                vals["openmes_default_line_id"] = line.id
        enabled = legacy[PARAM_ENABLED] == "True"
        try:
            with self.env.cr.savepoint():
                company.write(dict(vals, openmes_enabled=enabled))
        except ValidationError as exc:
            # Never log the values themselves: one of them is the API key.
            _logger.warning(
                "OpenMES settings of company %s were imported disabled: %s", company.id, exc.args[0]
            )
            vals.pop("openmes_api_url", None)
            vals.pop("openmes_api_key", None)
            vals.pop("openmes_order_prefix", None)
            company.write(dict(vals, openmes_enabled=False))
            self._openmes_write_unchecked(company, legacy)

    @api.model
    def _openmes_write_unchecked(self, company, legacy):
        """Keep an invalid URL / key / prefix visible in Settings (disabled) so
        the administrator can correct it, without passing the constraint."""
        columns = {
            "openmes_api_url": (legacy[PARAM_API_URL] or "").strip().rstrip("/") or None,
            "openmes_api_key": (legacy[PARAM_API_KEY] or "").strip() or None,
            "openmes_order_prefix": legacy[PARAM_ORDER_PREFIX] or DEFAULT_ORDER_PREFIX,
        }
        self.env.cr.execute(
            "UPDATE res_company SET openmes_api_url = %s, openmes_api_key = %s, openmes_order_prefix = %s "
            "WHERE id = %s",
            (columns["openmes_api_url"], columns["openmes_api_key"], columns["openmes_order_prefix"], company.id),
        )
        company.invalidate_recordset(list(columns))
