from odoo.tests import tagged

from .common import FakeResponse, OpenmesCase, import_report


@tagged("post_install", "-at_install")
class TestMasterData(OpenmesCase):
    def _bodies(self, calls):
        return {call["url"].rsplit("/api/v1/erp", 1)[1]: call["json"] for call in calls}

    def test_sync_master_data_sends_products_materials_and_recipes(self):
        with self.mock_openmes(
            import_report(imported=2),
            import_report(imported=2),
            import_report(updated=1),
        ) as calls:
            logs = self.env["openmes.sync"]._sync_master_data()

        bodies = self._bodies(calls)
        self.assertEqual(list(bodies), ["/products/import", "/materials/import", "/boms/import"])

        # No BoM has operations, so every product asks for a default template.
        self.assertTrue(bodies["/products/import"]["create_default_template"])
        products = {row["code"]: row for row in bodies["/products/import"]["products"]}
        self.assertEqual(set(products), {"BREAD-01", "CAKE-01"})
        self.assertEqual(products["BREAD-01"]["name"], "Rye bread")
        self.assertEqual(products["BREAD-01"]["external_system"], "odoo")
        self.assertEqual(products["BREAD-01"]["external_code"], str(self.bread.id))

        materials = {row["code"]: row for row in bodies["/materials/import"]["materials"]}
        self.assertEqual(set(materials), {"FLOUR-01", "YEAST-01"})
        self.assertEqual(materials["FLOUR-01"]["unit_price"], 2.5)
        self.assertEqual(materials["YEAST-01"]["tracking_type"], "batch")

        recipes = bodies["/boms/import"]
        self.assertEqual(recipes["mode"], "replace")
        self.assertEqual(len(recipes["recipes"]), 1, "the cake has no BoM")
        components = {c["material_code"]: c["quantity_per_unit"] for c in recipes["recipes"][0]["components"]}
        # BoM is for 10 breads: 5 kg flour, 100 g yeast → per one bread:
        self.assertEqual(components, {"FLOUR-01": 0.5, "YEAST-01": 0.01})

        self.assertEqual(logs.mapped("state"), ["success"] * 3)

    def test_bom_operations_are_sent_as_a_routing(self):
        workcenter = self.env["mrp.workcenter"].create({"name": "Oven", "default_capacity": 2})
        bom = self.env["mrp.bom"].search([("product_tmpl_id", "=", self.bread.product_tmpl_id.id)])
        bom.write({"operation_ids": [
            (0, 0, {"name": "Mixing", "workcenter_id": workcenter.id, "time_cycle_manual": 6, "sequence": 1}),
            (0, 0, {"name": "Baking", "workcenter_id": workcenter.id, "time_cycle_manual": 40, "sequence": 2,
                    "note": "<p>Bake at 220 °C</p>"}),
        ]})
        with self.mock_openmes(
            import_report(imported=1),  # cake: no operations → default template
            import_report(updated=1),   # bread: has a routing
            import_report(imported=1, rows=[{"row": 1, "code": "BREAD-01", "version": 2}]),
            import_report(imported=2),
            import_report(imported=1),
        ) as calls:
            logs = self.env["openmes.sync"]._sync_master_data()

        paths = [call["url"].rsplit("/api/v1/erp", 1)[1] for call in calls]
        self.assertEqual(paths, ["/products/import", "/products/import", "/process-templates/import",
                                 "/materials/import", "/boms/import"])
        self.assertEqual([r["code"] for r in calls[0]["json"]["products"]], ["CAKE-01"])
        self.assertTrue(calls[0]["json"]["create_default_template"])
        self.assertEqual([r["code"] for r in calls[1]["json"]["products"]], ["BREAD-01"])
        self.assertNotIn("create_default_template", calls[1]["json"])

        routing = calls[2]["json"]
        self.assertEqual(routing["mode"], "new_version")
        template = routing["templates"][0]
        self.assertEqual(template["product_type_code"], "BREAD-01")
        self.assertEqual([(s["step_number"], s["name"]) for s in template["steps"]], [(10, "Mixing"), (20, "Baking")])
        # Minutes per cycle / units per cycle.
        self.assertEqual([s["run_time_per_unit_minutes"] for s in template["steps"]], [3.0, 20.0])
        self.assertIn("220", template["steps"][1]["instruction"])
        self.assertIn("routings", logs.mapped("operation"))
        product_logs = logs.filtered(lambda l: l.operation == "products")
        self.assertEqual(len(product_logs), 1, "both product batches report as one entry")
        self.assertEqual((product_logs.sent_count, product_logs.imported_count, product_logs.updated_count), (2, 1, 1))

    def test_row_errors_and_missing_codes_are_logged(self):
        self.cake.default_code = False
        with self.mock_openmes(
            import_report(errors=[{"row": 1, "field": "code", "message": "boom"}]),
            import_report(imported=2),
            import_report(errors=[{"row": 1, "field": "product_type_code", "message": "No process template"}]),
        ):
            logs = self.env["openmes.sync"]._sync_master_data()

        product_log = logs.filtered(lambda l: l.operation == "products")
        self.assertEqual(product_log.state, "partial")
        self.assertEqual(product_log.error_count, 2)
        self.assertIn("BREAD-01: code: boom", product_log.message)
        self.assertIn("Cake", product_log.message)
        bom_log = logs.filtered(lambda l: l.operation == "boms")
        self.assertIn("BREAD-01: product_type_code: No process template", bom_log.message)

    def test_api_failure_is_logged_not_raised(self):
        with self.mock_openmes(
            FakeResponse(403, {"message": "Forbidden"}),
            import_report(imported=2),
            import_report(imported=1),
        ):
            logs = self.env["openmes.sync"]._sync_master_data()
        product_log = logs.filtered(lambda l: l.operation == "products")
        self.assertEqual(product_log.state, "failed")
        self.assertIn("scope", product_log.message)

    def test_recipe_with_uncoded_component_is_not_sent(self):
        self.yeast.default_code = False
        with self.mock_openmes(import_report(imported=2), import_report(imported=1)) as calls:
            logs = self.env["openmes.sync"]._sync_master_data()
        self.assertNotIn("/boms/import", self._bodies(calls))
        bom_log = logs.filtered(lambda l: l.operation == "boms")
        self.assertEqual(bom_log.sent_count, 0)
        self.assertIn("Yeast", bom_log.message)

    def test_stock_snapshot_per_warehouse(self):
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        warehouse.openmes_code = "0100"
        self.env["stock.quant"]._update_available_quantity(self.flour, warehouse.lot_stock_id, 340.25)
        self.env["stock.quant"]._update_available_quantity(self.bread, warehouse.lot_stock_id, 12)

        no_documents = FakeResponse(200, {"data": [], "meta": {"next_cursor": None}})
        with self.mock_openmes(no_documents, import_report(updated=4)) as calls:
            logs = self.env["openmes.sync"]._sync_all_stock()

        self.assertTrue(calls[0]["url"].endswith("/api/v1/erp/stock-documents"), "documents are booked first")
        body = calls[1]["json"]
        self.assertTrue(calls[1]["url"].endswith("/api/v1/erp/stock/import"))
        self.assertEqual(body["warehouse_code"], "0100")
        rows = {(r.get("product_type_code") or r.get("material_code")): r for r in body["balances"]}
        self.assertEqual(rows["FLOUR-01"]["quantity"], 340.25)
        self.assertEqual(rows["FLOUR-01"]["unit_of_measure"], self.uom_kg.name)
        self.assertEqual(rows["BREAD-01"]["quantity"], 12)
        self.assertIn("product_type_code", rows["BREAD-01"])
        self.assertEqual(rows["YEAST-01"]["quantity"], 0, "zero rows make the snapshot converge")
        self.assertEqual(rows["CAKE-01"]["quantity"], 0)
        self.assertEqual(logs.mapped("state"), ["success", "success"])

    def test_stock_skips_warehouses_without_code(self):
        self.env["stock.warehouse"].search([]).write({"openmes_code": False})
        with self.mock_openmes(FakeResponse(200, {"data": [], "meta": {"next_cursor": None}})):
            logs = self.env["openmes.sync"]._sync_all_stock()
        self.assertEqual(logs.mapped("operation"), ["stock_documents"])
