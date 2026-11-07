"""18.0.1.1.7: OpenMES settings move to the company.

Before the ORM makes ``company_id`` required on lines and stock documents,
every row gets a company: lines the main company (the only one connected so
far), stock documents the company of their warehouse, else the main company.
"""

from odoo.tools.sql import column_exists


def _main_company(cr):
    cr.execute(
        "SELECT res_id FROM ir_model_data WHERE module = 'base' AND name = 'main_company' AND model = 'res.company'"
    )
    row = cr.fetchone()
    if row:
        return row[0]
    cr.execute("SELECT MIN(id) FROM res_company")
    return cr.fetchone()[0]


def migrate(cr, version):
    if not version:
        return
    main = _main_company(cr)
    if not main:
        return
    if column_exists(cr, "openmes_line", "company_id"):
        cr.execute("UPDATE openmes_line SET company_id = %s WHERE company_id IS NULL", (main,))
    if column_exists(cr, "openmes_stock_document", "company_id"):
        cr.execute(
            """
            UPDATE openmes_stock_document d
               SET company_id = w.company_id
              FROM stock_warehouse w
             WHERE d.company_id IS NULL AND d.warehouse_id = w.id
            """
        )
        cr.execute("UPDATE openmes_stock_document SET company_id = %s WHERE company_id IS NULL", (main,))
