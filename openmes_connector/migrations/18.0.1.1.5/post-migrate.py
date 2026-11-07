"""18.0.1.1.5: typed OpenMES line codes become ``openmes.line`` records.

Products kept their line as free text in ``product_template.openmes_line_code``
(now computed from ``openmes_line_id``). Odoo leaves the old column in place,
so it is read here once and every code gets a line record; the next refresh
from OpenMES corrects the names and deactivates codes OpenMES does not know.
The default line stays a code in ir.config_parameter, it only needs its record.
"""

from odoo import SUPERUSER_ID, api
from odoo.tools.sql import column_exists


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    codes = {}
    if column_exists(cr, "product_template", "openmes_line_code"):
        cr.execute(
            "SELECT id, openmes_line_code FROM product_template "
            "WHERE openmes_line_code IS NOT NULL AND openmes_line_code <> ''"
        )
        codes = dict(cr.fetchall())
    default = env["ir.config_parameter"].get_param("openmes_connector.default_line_code")
    # The settings of 18.0.1.1.0 belong to the main company (see 18.0.1.1.7),
    # not to the company the superuser happens to be in.
    company = env["res.company"]._openmes_main_company()
    env["openmes.line"]._openmes_adopt_legacy_codes(codes, default, company=company)
