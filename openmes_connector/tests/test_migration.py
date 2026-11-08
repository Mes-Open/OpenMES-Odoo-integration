"""Data migrations of an upgrade from 18.0.1.1.0, run on the test database.

The whole upgrade (old module installed, data, ``-u``) is checked by
``scripts/test_upgrade.sh``; here the scripts run inside the test
transaction against a recreated 18.0.1.1.0 column.
"""

import importlib.util
import os

from odoo.modules import get_module_path
from odoo.tests import tagged

from .common import OpenmesCase


# Odoo 20: ir.config_parameter.get_param/set_param are gone; the typed
# get_str/set_str replace them (same stored text, same False default).
def _load(version, name):
    path = os.path.join(get_module_path("openmes_connector"), "migrations", version, f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"openmes_migration_{version.replace('.', '_')}_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged("post_install", "-at_install")
class TestMigrationFrom110(OpenmesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.main = cls.env.ref("base.main_company")
        cls.other = cls.env["res.company"].create({"name": "OdooBot's company"})
        cls.icp = cls.env["ir.config_parameter"].sudo()
        cls.mine = cls.env["product.product"].create({
            "name": "Main roll", "default_code": "MROLL", "company_id": cls.main.id, "openmes_manufacture": True,
        }).product_tmpl_id
        cls.shared = cls.env["product.product"].create({
            "name": "Shared roll", "default_code": "SROLL", "openmes_manufacture": True,
        }).product_tmpl_id

    def _legacy_database(self):
        """The 18.0.1.1.0 state: typed codes in a column, the default line a
        parameter, and the superuser working in another company."""
        cr = self.env.cr
        # DDL is transactional: rolled back with the test.
        cr.execute("ALTER TABLE product_template ADD COLUMN openmes_line_code varchar")
        cr.execute("UPDATE product_template SET openmes_line_code = 'M1' WHERE id = %s", (self.mine.id,))
        cr.execute("UPDATE product_template SET openmes_line_code = 'M2' WHERE id = %s", (self.shared.id,))
        self.main.write({"openmes_enabled": False, "openmes_api_url": False, "openmes_api_key": False,
                         "openmes_default_line_id": False})
        self.icp.set_str("openmes_connector.default_line_code", "MD")
        self.env.ref("base.user_root").write({"company_ids": [(4, self.other.id)], "company_id": self.other.id})
        self.env.invalidate_all()

    def test_lines_and_default_line_go_to_the_main_company(self):
        self._legacy_database()
        _load("18.0.1.1.5", "post-migrate").migrate(self.env.cr, "18.0.1.1.0")
        _load("18.0.1.1.7", "pre-migrate").migrate(self.env.cr, "18.0.1.1.0")
        _load("18.0.1.1.7", "post-migrate").migrate(self.env.cr, "18.0.1.1.0")
        self.env.invalidate_all()

        Line = self.env["openmes.line"].with_context(active_test=False)
        lines = Line.search([("code", "in", ("M1", "M2", "MD"))])
        self.assertEqual(sorted(lines.mapped("code")), ["M1", "M2", "MD"])
        self.assertEqual(lines.company_id, self.main, "not the superuser's company")
        self.assertFalse(Line.search_count([("company_id", "=", self.other.id)]))
        self.assertEqual(self.mine.openmes_line_id.code, "M1")
        self.assertEqual(self.shared.openmes_line_id.code, "M2")
        self.assertEqual(self.main.openmes_default_line_id.code, "MD")
        self.assertEqual(Line.search_count([("code", "=", "MD")]), 1, "the 1.1.7 step reuses the line")
        self.assertFalse(self.icp.get_str("openmes_connector.default_line_code", False))

    def test_fresh_install_does_nothing(self):
        self._legacy_database()
        before = self.env["openmes.line"].with_context(active_test=False).search_count([])
        for version, name in (("18.0.1.1.5", "post-migrate"), ("18.0.1.1.7", "pre-migrate"),
                              ("18.0.1.1.7", "post-migrate")):
            _load(version, name).migrate(self.env.cr, None)
        self.assertEqual(self.env["openmes.line"].with_context(active_test=False).search_count([]), before)
        self.assertFalse(self.mine.openmes_line_id)
        self.assertEqual(self.icp.get_str("openmes_connector.default_line_code", False), "MD")
