"""18.0.1.1.7: the global OpenMES settings (ir.config_parameter) move to the
main company and the parameters are deleted; see
``res.company._openmes_adopt_legacy_params``."""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.company"]._openmes_adopt_legacy_params()
