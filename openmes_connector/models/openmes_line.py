"""OpenMES production lines, read from ``GET /api/v1/erp/lines``.

OpenMES owns the lines; Odoo keeps a copy so the default line in Settings and
the line of a product can be picked from a list instead of typed. The refresh
always reads the whole list (cursor by cursor, without ``since``): OpenMES
does not list deleted lines, so only a complete list tells which lines are
gone. Lines missing from it, or inactive in OpenMES, become inactive here.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .openmes_client import OpenmesApiError, parse_iso_datetime

_logger = logging.getLogger(__name__)

MAX_CODE_LENGTH = 100
MAX_NAME_LENGTH = 255
MAX_LOGGED_ERRORS = 200


def clean_line_code(value):
    """The code of a line row, or None when it is not a usable code."""
    if not isinstance(value, str):
        return None
    code = value.strip()
    if not code or len(code) > MAX_CODE_LENGTH or code != value:
        return None
    return code


class OpenmesLine(models.Model):
    _name = "openmes.line"
    _description = "OpenMES Production Line"
    _order = "code"
    _rec_names_search = ["code", "name"]

    code = fields.Char(required=True, index=True, readonly=True)
    name = fields.Char(required=True, readonly=True)
    description = fields.Text(readonly=True)
    active = fields.Boolean(default=True, readonly=True)
    company_id = fields.Many2one(
        "res.company",
        "Company",
        required=True,
        readonly=True,
        index=True,
        default=lambda self: self.env.company,
        help="The company whose OpenMES instance has this line.",
    )
    openmes_updated_at = fields.Datetime("Changed in OpenMES", readonly=True)
    last_refresh = fields.Datetime("Last Refresh", readonly=True)

    _sql_constraints = [
        # Same name as before: two OpenMES instances may both have a line "L1".
        ("code_uniq", "unique(code, company_id)", "An OpenMES line with this code already exists in this company."),
    ]

    @api.constrains("code")
    def _check_code(self):
        for line in self:
            if clean_line_code(line.code) is None:
                raise ValidationError(
                    _("The OpenMES line code must be at most 100 characters, without leading or trailing spaces.")
                )

    @api.depends("code", "name")
    def _compute_display_name(self):
        for line in self:
            if line.name and line.name != line.code:
                line.display_name = f"[{line.code}] {line.name}"
            else:
                line.display_name = line.code or ""

    @api.ondelete(at_uninstall=False)
    def _unlink_except_default(self):
        companies = self.env["res.company"].sudo().search([("openmes_default_line_id", "in", self.ids)], limit=1)
        if companies:
            raise UserError(
                _(
                    "Line “%s” is the default OpenMES line in Settings; choose another default first.",
                    companies.openmes_default_line_id.code,
                )
            )

    # ── Refresh ──────────────────────────────────────────────────────────────

    @api.model
    def _openmes_parse_rows(self, rows):
        """Return ``({code: values}, errors)`` from the rows of ``/erp/lines``."""
        parsed, errors = {}, []
        for row in rows:
            code = clean_line_code(row.get("code")) if isinstance(row, dict) else None
            if code is None:
                errors.append(_("?: invalid line row"))
                continue
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                name = code
            description = row.get("description")
            active = row.get("is_active")
            parsed[code] = {
                "name": name.strip()[:MAX_NAME_LENGTH],
                "description": description if isinstance(description, str) and description else False,
                "active": active if isinstance(active, bool) else True,
                "openmes_updated_at": parse_iso_datetime(row.get("updated_at")),
            }
        return parsed, errors

    @api.model
    def _openmes_failed_log(self, message):
        _logger.warning("OpenMES line refresh failed: %s", message)
        return self.env["openmes.sync.log"].sudo().create(
            {
                "operation": "lines",
                "state": "failed",
                "error_count": 1,
                "message": message,
                "company_id": self.env.company.id,
            }
        )

    @api.model
    def _openmes_refresh(self):
        """Mirror the OpenMES line list of ``self.env.company``. Nothing changes
        unless every page was read; lines of other companies are never touched."""
        company = self.env.company
        Line = self.sudo().with_company(company).with_context(active_test=False)
        try:
            rows = list(self.env["openmes.client"].sudo().with_company(company)._iter_export("/lines"))
        except OpenmesApiError as exc:
            return self.with_company(company)._openmes_failed_log(
                _("Production lines could not be read from OpenMES: %s", exc)
            )

        parsed, errors = self._openmes_parse_rows(rows)
        now = fields.Datetime.now()
        existing = {line.code: line for line in Line.search([("company_id", "=", company.id)])}
        created = updated = 0
        for code, values in parsed.items():
            line = existing.get(code)
            if not line:
                Line.create(dict(values, code=code, company_id=company.id, last_refresh=now))
                created += 1
                continue
            changes = {
                key: value for key, value in values.items()
                if (line[key] or False) != (value or False)
            }
            if changes:
                updated += 1
            line.write(dict(changes, last_refresh=now))

        gone = Line.browse([
            line.id for code, line in existing.items() if code not in parsed and line.active
        ])
        gone.write({"active": False, "last_refresh": now})

        message = "\n".join(errors[:MAX_LOGGED_ERRORS])
        if len(errors) > MAX_LOGGED_ERRORS:
            message += "\n" + _("… and %s more.", len(errors) - MAX_LOGGED_ERRORS)
        return self.env["openmes.sync.log"].sudo().create({
            "operation": "lines",
            "company_id": company.id,
            "state": "partial" if errors else "success",
            "sent_count": len(rows),
            "imported_count": created,
            "updated_count": updated,
            "skipped_count": len(gone),
            "error_count": len(errors),
            "message": message or False,
        })

    @api.model
    def _cron_openmes_refresh_lines(self):
        self.env["res.company"]._openmes_cron_each(
            "line refresh", lambda company: self.sudo().with_company(company)._openmes_refresh()
        )

    # ── Migration ────────────────────────────────────────────────────────────

    @api.model
    def _openmes_adopt_legacy_codes(self, codes_by_template, default_code=None, company=None):
        """Turn typed line codes into line records (upgrade from 18.0.1.1.4).

        ``codes_by_template``: ``{product.template id: code}``. Idempotent. The
        records get the code as name and belong to ``company`` (default: the
        current one); the next refresh corrects the name or deactivates a line
        OpenMES does not know. A product of another company keeps no line: it
        uses the default line of its company."""
        company = company or self.env.company
        Line = self.sudo().with_context(active_test=False)
        templates_by_code = {}
        for template_id, raw in (codes_by_template or {}).items():
            code = (raw or "").strip() if isinstance(raw, str) else ""
            if not code:
                continue
            if len(code) > MAX_CODE_LENGTH:
                _logger.warning("OpenMES line code of product template %s is too long; not migrated.", template_id)
                continue
            templates_by_code.setdefault(code, []).append(template_id)
        default = (default_code or "").strip() if isinstance(default_code, str) else ""
        codes = set(templates_by_code)
        if default and len(default) <= MAX_CODE_LENGTH:
            codes.add(default)

        lines = {
            line.code: line
            for line in Line.search([("code", "in", list(codes)), ("company_id", "=", company.id)])
        }
        for code in sorted(codes - set(lines)):
            lines[code] = Line.create({"code": code, "name": code, "company_id": company.id})
        Template = self.env["product.template"].sudo().with_context(active_test=False)
        for code, template_ids in templates_by_code.items():
            templates = Template.browse(template_ids).exists().filtered(
                lambda t: t.company_id in (company, self.env["res.company"])
            )
            templates.write({"openmes_line_id": lines[code].id})
        return Line.browse([line.id for line in lines.values()])
