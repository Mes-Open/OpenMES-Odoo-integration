import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests.common import TransactionCase

REQUEST_PATH = "odoo.addons.openmes_connector.models.openmes_client.requests.request"
API_URL = "https://mes.example.test"
API_KEY = "omk_" + "a" * 40


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return json.loads(json.dumps(self._body))


def import_report(imported=0, updated=0, skipped=0, errors=None, status=None, results=None, rows=None):
    errors = errors or []
    data = {"imported": imported, "updated": updated, "skipped": skipped, "errors": errors}
    if results is not None:
        data["results"] = results
    if rows is not None:
        data["rows"] = rows
    return FakeResponse(status or (207 if errors else 200), {"data": data})


def lines_page(rows, next_cursor=None):
    return FakeResponse(200, {"data": rows, "meta": {"next_cursor": next_cursor}})


class OpenmesCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uom_unit = cls.env.ref("uom.product_uom_unit")
        cls.uom_dozen = cls.env.ref("uom.product_uom_dozen")
        cls.uom_kg = cls.env.ref("uom.product_uom_kgm")
        cls.uom_gram = cls.env.ref("uom.product_uom_gram")

        cls.line_l1 = cls.env["openmes.line"].create({"code": "L1", "name": "Line one"})
        cls.line_l2 = cls.env["openmes.line"].create({"code": "L2", "name": "Line two"})
        cls.env.company.write({
            "openmes_enabled": True,
            "openmes_api_url": API_URL,
            "openmes_api_key": API_KEY,
            "openmes_default_line_id": cls.line_l1.id,
            "openmes_order_prefix": "ODOO",
        })

        cls.partner = cls.env["res.partner"].create({"name": "Bakery Customer"})
        cls.flour = cls.env["product.product"].create({
            "name": "Rye flour", "default_code": "FLOUR-01", "is_storable": True,
            "uom_id": cls.uom_kg.id, "standard_price": 2.5,
        })
        cls.yeast = cls.env["product.product"].create({
            "name": "Yeast", "default_code": "YEAST-01", "is_storable": True,
            "uom_id": cls.uom_kg.id, "tracking": "lot",
        })
        cls.bread = cls.env["product.product"].create({
            "name": "Rye bread", "default_code": "BREAD-01", "is_storable": True,
            "openmes_manufacture": True, "list_price": 4.0,
        })
        cls.cake = cls.env["product.product"].create({
            "name": "Cake", "default_code": "CAKE-01", "openmes_manufacture": True,
            "openmes_line_id": cls.line_l2.id, "list_price": 10.0,
        })
        cls.service = cls.env["product.product"].create({"name": "Delivery", "type": "service", "list_price": 5})
        # 10 breads need 5 kg flour and 100 g yeast.
        cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.bread.product_tmpl_id.id,
            "product_qty": 10,
            "bom_line_ids": [
                (0, 0, {"product_id": cls.flour.id, "product_qty": 5}),
                (0, 0, {"product_id": cls.yeast.id, "product_qty": 100, "product_uom_id": cls.uom_gram.id}),
            ],
        })

    @classmethod
    def _configure(cls, company=None, **vals):
        """Write OpenMES settings (``enabled=False``, ``order_source=…``) on a company."""
        company = company or cls.env.company
        company.sudo().write({f"openmes_{key}": value for key, value in vals.items()})

    @classmethod
    def _has_operation_note(cls):
        """Odoo 19 Community dropped the operation description (``note``)."""
        return "note" in cls.env["mrp.routing.workcenter"]._fields

    @classmethod
    def _operation_note(cls, html):
        return {"note": html} if cls._has_operation_note() else {}

    def _create_order(self, lines):
        return self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [
                (0, 0, {"product_id": product.id, "product_uom_qty": qty, **extra})
                for product, qty, extra in lines
            ],
        })

    @contextmanager
    def mock_openmes(self, *responses):
        """Patch HTTP; each call pops the next response (or calls it with the request)."""
        queue = list(responses)
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            if not queue:
                raise AssertionError(f"Unexpected OpenMES call {method} {url}")
            response = queue.pop(0)
            return response(method, url, **kwargs) if callable(response) else response

        with patch(REQUEST_PATH, side_effect=fake_request):
            yield calls
        self.assertFalse(queue, "Not every mocked OpenMES response was consumed")
