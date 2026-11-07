import struct

from odoo.tests import TransactionCase, tagged
from odoo.tools.misc import file_open, file_path

ICON = "openmes_connector/static/description/icon.png"
INDEX = "openmes_connector/static/description/index.html"


@tagged("post_install", "-at_install")
class TestModuleDescription(TransactionCase):
    def test_icon_is_128_png(self):
        with open(file_path(ICON), "rb") as icon:
            data = icon.read()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(data[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", data[16:24]), (128, 128))

    def test_module_record_uses_icon(self):
        module = self.env["ir.module.module"].search([("name", "=", "openmes_connector")])
        self.assertEqual(module.icon, "/openmes_connector/static/description/icon.png")

    def test_description_page_is_static(self):
        with file_open(INDEX) as page:
            html = page.read().lower()
        for forbidden in ("<script", "<iframe", 'src="http', "<link", "<style"):
            with self.subTest(tag=forbidden):
                self.assertNotIn(forbidden, html)
        module = self.env["ir.module.module"].search([("name", "=", "openmes_connector")])
        self.assertIn("OpenMES", module.description_html)
