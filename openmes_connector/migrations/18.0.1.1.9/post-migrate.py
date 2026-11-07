"""18.0.1.1.9: webhook deliveries become queued ``openmes.webhook.event`` records.

``openmes.webhook.delivery`` was the replay ledger (delivery ids kept 24 h).
Its rows of the last 24 hours are copied as processed events, so a delivery
OpenMES retries across the upgrade is still answered as a duplicate. Odoo
drops the old model and its table after the update.
"""

from odoo.tools.sql import table_exists


def migrate(cr, version):
    if not version or not table_exists(cr, "openmes_webhook_delivery"):
        return
    cr.execute(
        """
        INSERT INTO openmes_webhook_event
               (delivery_id, event, company_id, received_at, processed_at, state, result,
                attempt_count, create_uid, create_date, write_uid, write_date)
        SELECT delivery_id, COALESCE(event, ''), company_id, received_at, received_at, 'done', 'processed',
               0, 1, now() AT TIME ZONE 'UTC', 1, now() AT TIME ZONE 'UTC'
          FROM openmes_webhook_delivery
         WHERE company_id IS NOT NULL
           AND received_at > (now() AT TIME ZONE 'UTC') - interval '24 hours'
        ON CONFLICT DO NOTHING
        """
    )
