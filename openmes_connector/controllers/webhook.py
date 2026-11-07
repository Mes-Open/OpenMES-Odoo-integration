import logging

from odoo import SUPERUSER_ID, http
from odoo.http import Response, request

from ..models.openmes_client import WEBHOOK_MAX_BODY, WEBHOOK_PATH
from ..models.openmes_webhook import WebhookRejected

_logger = logging.getLogger(__name__)


class OpenmesWebhookController(http.Controller):
    @http.route(
        WEBHOOK_PATH,
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
        max_content_length=WEBHOOK_MAX_BODY,
    )
    def openmes_webhook(self, **kwargs):
        httprequest = request.httprequest
        length = httprequest.content_length
        if length is None or length > WEBHOOK_MAX_BODY:
            return Response(status=413)
        raw_body = httprequest.get_data(cache=False)
        if len(raw_body) > WEBHOOK_MAX_BODY:
            return Response(status=413)
        # Like the crons: OdooBot authors the chatter messages, not the public user.
        env = request.env(user=SUPERUSER_ID, su=True)
        try:
            status, payload = env["openmes.webhook"]._openmes_receive(httprequest.headers, raw_body)
        except WebhookRejected as exc:
            # Only a short reason: never the signature, the secret or the body.
            _logger.warning("OpenMES webhook from %s rejected: %s", httprequest.remote_addr, exc)
            return Response(status=401)
        return request.make_json_response(payload, status=status)
