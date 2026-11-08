"""HTTP client for the OpenMES ERP integration API (``/api/v1/erp``).

The ERP API is machine-to-machine: it authenticates with a per-integration API
key (``omk_…``) sent in the ``X-Api-Key`` header and authorizes every endpoint
by scope. Every ``*/import`` endpoint answers with a per-row report
(``imported`` / ``updated`` / ``skipped`` / ``errors``) and returns ``200`` when
all rows were applied or ``207`` when some failed; ``422`` means the payload
itself was malformed.

All methods are private so they can never be reached through RPC.
"""

import logging
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import psycopg2
import requests

from odoo import _, api, models

_logger = logging.getLogger(__name__)

# Another transaction changed or holds the same rows (a webhook event and a
# send on one work order): not a business error, the same work succeeds when
# it is run again in a new transaction.
CONCURRENCY_ERRORS = (
    psycopg2.errors.SerializationFailure,
    psycopg2.errors.LockNotAvailable,
    psycopg2.errors.DeadlockDetected,
)

# Global settings up to 18.0.1.1.6; read only by the migration to res.company.
PARAM_ENABLED = "openmes_connector.enabled"
PARAM_API_URL = "openmes_connector.api_url"
PARAM_API_KEY = "openmes_connector.api_key"
PARAM_DEFAULT_LINE = "openmes_connector.default_line_code"
PARAM_ORDER_PREFIX = "openmes_connector.order_prefix"
PARAM_STATUS_SINCE = "openmes_connector.status_since"
PARAM_CR_SINCE = "openmes_connector.change_request_since"
PARAM_ORDER_SOURCE = "openmes_connector.order_source"
PARAM_WEBHOOK_ID = "openmes_connector.webhook_id"
PARAM_WEBHOOK_SECRET = "openmes_connector.webhook_secret"
PARAM_WEBHOOK_URL = "openmes_connector.webhook_url"

# Webhooks pushed by OpenMES (``/openmes/webhook``).
WEBHOOK_PATH = "/openmes/webhook"
WEBHOOK_EVENTS = (
    "work_order.created",
    "work_order.status_changed",
    "work_order.progress",
    "stock_document.posted",
    "quality_issue.created",
)
# Maximum distance between X-OpenMES-Timestamp and now, in seconds.
WEBHOOK_TOLERANCE = 300
WEBHOOK_MAX_BODY = 1024 * 1024

# Where OpenMES work orders come from.
SOURCE_SALE_LINES = "sale_lines"
SOURCE_MANUFACTURING = "manufacturing_orders"
ORDER_SOURCES = (SOURCE_SALE_LINES, SOURCE_MANUFACTURING)

API_KEY_PREFIX = "omk_"
DEFAULT_ORDER_PREFIX = "ODOO"
REQUEST_TIMEOUT = 20
EXPORT_PAGE_SIZE = 500
USER_AGENT = "odoo-openmes-connector/19.0.1.0.0"

# Scopes the module uses, in the order Test Connection lists them.
REQUIRED_SCOPES = (
    "erp:orders:import",
    "erp:production:read",
    "erp:masterdata:read",
    "erp:masterdata:write",
    "erp:stock:read",
    "erp:stock:write",
    "erp:webhooks:manage",
)


class OpenmesApiError(Exception):
    """A request to OpenMES failed as a whole (transport, auth, 4xx/5xx)."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def validate_api_url(env, url):
    """Return an error message when ``url`` is not a usable OpenMES base URL."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return env._("The OpenMES URL must start with http:// or https:// and name a host.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return env._("The OpenMES URL must not contain credentials, a query string or a fragment.")
    return None


def parse_iso_datetime(value):
    """Parse an ISO 8601 timestamp from OpenMES into a naive UTC datetime."""
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class OpenmesClient(models.AbstractModel):
    _name = "openmes.client"
    _description = "OpenMES ERP API client"

    # The configuration is the one of ``self.env.company``: callers pick the
    # company with ``with_company`` (the record's company, or each enabled
    # company in the crons).

    @api.model
    def _company(self):
        return self.env.company.sudo()

    @api.model
    def _is_enabled(self):
        return bool(self._company().openmes_enabled)

    @api.model
    def _order_source(self):
        """``sale_lines`` (default) or ``manufacturing_orders``."""
        source = self._company().openmes_order_source
        return source if source in ORDER_SOURCES else SOURCE_SALE_LINES

    @api.model
    def _get_connection(self, api_url=None, api_key=None):
        company = self._company()
        api_url = (api_url if api_url is not None else company.openmes_api_url) or ""
        api_key = (api_key if api_key is not None else company.openmes_api_key) or ""
        api_url = api_url.strip().rstrip("/")
        api_key = api_key.strip()
        if not api_url or not api_key:
            raise OpenmesApiError(_(
                "OpenMES is not configured for company %s: set the API URL and API key in Settings.",
                company.name,
            ))
        error = validate_api_url(self.env, api_url)
        if error:
            raise OpenmesApiError(error)
        return api_url, api_key

    @api.model
    def _request(self, method, path, payload=None, params=None, api_url=None, api_key=None, allow_empty=False):
        base_url, key = self._get_connection(api_url, api_key)
        url = f"{base_url}/api/v1/erp{path}"
        headers = {
            "X-Api-Key": key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        try:
            response = requests.request(
                method,
                url,
                json=payload,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # The exception text can carry the URL but never the key (sent as a header).
            _logger.warning("OpenMES request %s %s failed: %s", method, path, exc)
            raise OpenmesApiError(_("Cannot reach OpenMES (%s).", type(exc).__name__)) from exc

        return self._handle_response(method, path, response, allow_empty=allow_empty)

    @api.model
    def _handle_response(self, method, path, response, allow_empty=False):
        """``allow_empty``: a 2xx answer without a JSON body counts as success.
        Only for writes whose answer carries nothing we need (acknowledgements);
        reads and imports must stay strict, or an HTML page answering 200
        (proxy, maintenance page, wrong URL) would look like an empty result."""
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None

        if status in (200, 201, 202, 207) and isinstance(body, dict):
            return body
        if allow_empty and status in (200, 201, 202, 204) and body is None:
            return {}

        _logger.warning("OpenMES request %s %s returned HTTP %s", method, path, status)
        messages = {
            401: _("OpenMES rejected the API key (missing, inactive or expired)."),
            403: _("The OpenMES API key lacks the scope required for this operation."),
            404: _(
                "OpenMES endpoint not found. Check that the ERP integration module "
                "(and Warehouses, for stock) is enabled in OpenMES."
            ),
            429: _("OpenMES rate limit exceeded; the operation will be retried later."),
        }
        if status in messages:
            raise OpenmesApiError(messages[status], status)
        if status == 409:
            # A state conflict the caller handles (e.g. cancelling a finished order).
            detail = body.get("message") if isinstance(body, dict) else None
            raise OpenmesApiError(detail or _("OpenMES refused the change in the order's current state."), status)
        if status == 422 and isinstance(body, dict):
            details = []
            for field, errors in (body.get("errors") or {}).items():
                details.append(f"{field}: {', '.join(map(str, errors))}")
            detail = "; ".join(details[:10]) or body.get("message") or ""
            raise OpenmesApiError(_("OpenMES rejected the payload: %s", detail), status)
        if 300 <= status < 400:
            raise OpenmesApiError(
                _("OpenMES answered with a redirect (HTTP %s). Check the URL (http vs https).", status),
                status,
            )
        raise OpenmesApiError(_("Unexpected response from OpenMES (HTTP %s).", status), status)

    # ── Endpoints ────────────────────────────────────────────────────────────

    @api.model
    def _post_import(self, path, body):
        """POST an import payload and return its normalised per-row report."""
        response = self._request("POST", path, payload=body)
        data = response.get("data") or {}
        return {
            "imported": int(data.get("imported") or 0),
            "updated": int(data.get("updated") or 0),
            "skipped": int(data.get("skipped") or 0),
            "errors": [err for err in (data.get("errors") or []) if isinstance(err, dict)],
            # Per-row outcome (work orders) and per-row notes (master data);
            # absent on older OpenMES versions.
            "results": [res for res in (data.get("results") or []) if isinstance(res, dict)],
            "rows": [row for row in (data.get("rows") or []) if isinstance(row, dict)],
            "warnings": [w for w in (data.get("warnings") or []) if isinstance(w, dict)],
        }

    @api.model
    def _cancel_work_order(self, order_no, reason=None):
        """Ask OpenMES to cancel ``order_no``.

        Returns the response ``data``: ``result`` is ``cancelled`` (applied at
        once) or ``change_requested`` (the order is in production, so a manager
        decides). Raises OpenmesApiError with status 409 when the order is
        already finished.
        """
        body = {"reason": reason[:1000]} if reason else {}
        response = self._request(
            "POST",
            f"/work-orders/{quote(order_no, safe='')}/cancel",
            payload=body,
            params={"by": "order_no"},
        )
        return response.get("data") or {}

    @api.model
    def _get_work_order_settlement(self, order_no):
        """``data`` of ``GET /work-orders/{order_no}`` with the settlement
        sections (consumption, scrap, costs)."""
        response = self._request(
            "GET",
            f"/work-orders/{quote(order_no, safe='')}",
            params={"by": "order_no", "include": "consumption,scrap,costs"},
        )
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    @api.model
    def _ack_stock_document(self, mes_id, erp_reference=None):
        """Mark OpenMES stock document ``mes_id`` as booked in the ERP."""
        body = {"erp_reference": erp_reference[:255]} if erp_reference else {}
        return self._request("POST", f"/stock-documents/{int(mes_id)}/ack", payload=body, allow_empty=True)

    @api.model
    def _register_webhook(self, name, url, events):
        """Register a webhook owned by this API key. OpenMES generates the
        secret and returns it only in this answer: ``{"data": {...}, "secret": …}``."""
        return self._request(
            "POST",
            "/webhooks",
            payload={"name": name[:255], "url": url, "events": list(events), "is_active": True},
        )

    @api.model
    def _delete_webhook(self, webhook_id):
        """Delete (soft-delete) webhook ``webhook_id``; OpenMES answers 204."""
        return self._request("DELETE", f"/webhooks/{int(webhook_id)}", allow_empty=True)

    @api.model
    def _iter_export(self, path, params=None):
        """Yield every row of a cursor-paginated export endpoint."""
        params = dict(params or {}, limit=EXPORT_PAGE_SIZE)
        while True:
            response = self._request("GET", path, params=dict(params))
            yield from response.get("data") or []
            next_cursor = (response.get("meta") or {}).get("next_cursor")
            if not next_cursor:
                return
            params["cursor"] = next_cursor

    @api.model
    def _get_key_info(self, api_url, api_key):
        """``data`` of ``GET /me`` (what the key may do), or None when this
        OpenMES version has no such endpoint (404). Other errors are raised."""
        try:
            response = self._request("GET", "/me", api_url=api_url, api_key=api_key)
        except OpenmesApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return response.get("data")

    @api.model
    def _test_connection(self, api_url, api_key):
        """Raise OpenmesApiError unless the URL and key reach the ERP API."""
        self._request(
            "GET",
            "/production/completions",
            params={"limit": 1},
            api_url=api_url,
            api_key=api_key,
        )
