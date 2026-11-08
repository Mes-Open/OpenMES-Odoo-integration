=================
OpenMES Connector
=================

Connects Odoo 20 to `OpenMES <https://github.com/Mes-Open/OpenMes>`_ through
the OpenMES ERP integration API (``/api/v1/erp``).

Requirements: **Odoo 20.0** (Community or Enterprise) with Sales
(``sale_management``), Inventory (``sale_stock``) and Manufacturing (``mrp``).
This is the 20.0 branch of the module; Odoo 18.0 and 19.0 have their own branches.

Features
========

* **Two sources of work orders** (**Settings > OpenMES > Create OpenMES work
  orders from**):

  - *Sale order lines* (default): confirming a sale order that contains a
    product flagged *Manufacture in OpenMES* queues one OpenMES work order per
    order line. Use it when Odoo MRP does not plan production (make to order,
    OpenMES is the only place where production happens).
  - *Manufacturing orders*: confirming a manufacturing order (MO) of a flagged
    product queues one OpenMES work order for that MO; sale order lines create
    none, so there is never a second order for the same production. Use it
    with Odoo MRP: the *Manufacture* + *MTO* routes, reordering rules and
    producing for stock. When the MO was made for a sale order, the customer
    and its reference are sent and the work order also shows on that sale
    order.

  A cron sends the queue right after the confirmation commits, so confirming
  never waits on OpenMES or fails because of it. Switching the mode never
  changes existing work orders: they keep following their own sale order or
  MO.
* **Changes and cancellations**: changing the quantity (or the commitment
  date) of a confirmed sale order, or the quantity or deadline of an MO,
  resends its work order, and cancelling the sale order or MO cancels it in
  OpenMES. Before production starts OpenMES applies
  this at once; once the order is in production it becomes a change request
  that a manager approves or rejects in OpenMES — the decision comes back to
  Odoo and is posted on the sale order (or MO).
* **Production feedback**: the OpenMES status, planned and produced quantity
  and completion time are pulled back every 15 minutes (one request for all
  statuses) and posted on the sale order (or MO) chatter.
* **Webhooks**: with a webhook registered (**Settings > OpenMES > Register
  Webhook**) OpenMES pushes ``work_order.created``,
  ``work_order.status_changed`` and ``work_order.progress`` to
  ``/openmes/webhook`` and Odoo applies them with the same code as the pull.
  ``stock_document.posted`` starts the stock document booking (the documents
  are still fetched from OpenMES, never taken from the event), and
  ``quality_issue.created`` posts a note on the work order. Every
  delivery must carry a valid ``X-OpenMES-Signature`` (HMAC-SHA256 over
  ``timestamp.body`` with the secret OpenMES returned at registration), a
  timestamp at most 5 minutes off and a delivery id; anything else gets an
  empty ``401`` and changes nothing. An authentic delivery is only stored
  and answered ``200`` with ``{"status": "queued"}`` at once; the
  *OpenMES: process webhook events* cron (started by the delivery, every 5
  minutes as a fallback) then applies the events one by one in the order they
  arrived, each in its own transaction, so closing a large MO never runs
  against OpenMES' 10-second answer limit. A delivery whose id was already
  received (OpenMES retries when it did not get the answer in time) gets
  ``200`` with ``{"status": "duplicate"}`` and changes nothing, so OpenMES
  does not show it as failed; two copies arriving at the same moment are
  queued once. A body that is not a JSON object with a ``data`` object gets
  ``400``, bodies over 1 MiB ``413``; ``webhook.test``, unknown events and
  deliveries for a company with the integration off get ``200`` with
  ``{"status": "ignored"}`` and are not stored. Events are listed in
  **OpenMES > Webhook Events** (also **Manufacturing > OpenMES Webhook
  Events**; sales and manufacturing managers read, administrators retry):
  an event that waits for a work order another transaction is changing (a
  send in progress) stays *Pending* and is tried again 30 seconds later
  without counting an attempt; an error (e.g. Odoo refused a change) is tried
  5 times, 5 minutes apart, and then stays in *Error* (filter *Errors*) with
  its message until an administrator clicks **Retry**; an event with invalid
  values (e.g. a non-numeric quantity) goes to *Error* at once. Processed
  events are deleted after 7 days; pending and failed ones are kept. The
  payload is shown on the event but never written to the server log. An
  event is processed in the
  company whose secret signed it (its MO's pickings, backorder and
  activities). An older state (a late retry) never overwrites a newer one
  (``updated_at``). Polling stays as a fallback: while a webhook is
  registered the status pull runs every hour instead of every 15 minutes.
* **Closing the MO**: when OpenMES reports a work order made from an MO as
  *Done*, Odoo sets the MO's quantity producing to the quantity OpenMES
  produced and marks the MO as done. If OpenMES produced less than planned, a
  backorder MO is created for the rest (the standard *Create Backorder*
  answer, without the wizard) and gets its own OpenMES work order; the
  original work order's planned quantity becomes the quantity made, so it is
  no quantity difference (the backorder carries the rest). Closing the MO also
  starts the stock document run at once, so the MO's documents that were
  waiting for it are acknowledged and stop holding back the snapshot. If Odoo
  cannot close the MO (e.g. a lot number is missing), the error is posted on
  the MO and the work order; fix the cause and click **Close Manufacturing
  Order** on the work order. A backorder or split made in Odoo gets its own
  work order only when the original MO is in OpenMES, so an MO created before
  switching modes never reaches OpenMES. An MO whose work order is still open
  in OpenMES (or whose cancellation is not sent yet) cannot be deleted.
* **Lot of the finished product**: OpenMES does not report lot numbers of
  finished products. When the finished product is tracked by lots and the MO
  has no lot yet, Odoo creates one with its standard lot numbering (the same
  as the MO's *Generate* button) before closing the MO, and posts it on the
  MO and the work order; the close message names the lot too. A lot set by
  hand on the MO before the close is used instead. The lot is kept even if
  the close then fails (e.g. a component lot is missing), so **Close
  Manufacturing Order** reuses it. A backorder gets its own lot when it is
  closed. The work order form shows it as *Finished Product Lot* (with
  several lots or serial numbers on the MO, as Odoo 19 and later allow, all of them,
  comma-separated). Products
  tracked by serial numbers are never closed automatically: the work order's
  last error and a message on the MO say so, and the MO's responsible (or the
  salesperson of the sale order) gets one To-Do to assign the serial numbers
  and close the MO in Odoo. Once the MO is marked as done (by OpenMES or by
  hand), the "could not close" error of its finished work orders is cleared.
* **Quantity differences**: Odoo never corrects a document on its own when
  OpenMES ends up with another quantity; a person gets a *To-Do* activity on
  the sale order (the salesperson) or, for an MO work order, on the MO (its
  responsible, else the salesperson of the sale order it was made for):

  - when a manager in OpenMES rejects a change or a cancellation — with what
    was rejected, the reason from OpenMES and the quantity OpenMES keeps
    planning or producing; one activity per change request, also after later
    pulls or after the activity was marked done;
  - when OpenMES finishes a work order with more or less than planned in Odoo
    (compared at the *Product Unit* decimal precision, which Odoo 19 and later
    use for every unit of measure) — *check
    the delivery and the invoice*, with both quantities and the difference.

  The work order is flagged with a *Quantity Difference* (OpenMES plans a
  different quantity once no change request is open, or produced a different
  quantity) until the order is adjusted or the OpenMES quantity accepted.
* **Settlement**: once a work order is *Done* in OpenMES (from the pull or a
  webhook), a cron fetches its settlement once
  (``GET /api/v1/erp/work-orders/{order_no}?by=order_no&include=consumption,scrap,costs``)
  and stores it on the work order: the scrap quantity, the total cost and the
  cost per unit with their currency, and the material consumption (material,
  lots, consumed, scrapped and returned quantity, unit). A summary is posted
  on the sale order (or MO). The status pull and the webhook only mark the
  order *pending*, so they never wait for the settlement and a failed fetch
  never undoes the status; a failure is retried every 15 minutes up to five
  times. The figures are information only: nothing is booked in accounting,
  unless the MO cost option below is on.
* **OpenMES costs in the MO cost** (manufacturing orders mode, per company,
  off by default): when an MO in OpenMES is closed, its *Extra Unit Cost*
  (``mrp_account``) is set to (OpenMES labour + additional costs) ÷ the
  quantity being closed, so the finished product's valuation includes them.
  The settlement is fetched during the close if it is not stored yet; if
  that fails, the MO is closed as before and its chatter says the costs were
  not added. OpenMES material costs are never added (Odoo values the
  components itself).
* **Master data**: flagged products become OpenMES product types, the
  operations of their BoM become the OpenMES routing (process template, a new
  version per change), their BoM components become materials, and each BoM
  becomes a recipe (quantity per one unit, ``replace`` mode). A product whose
  BoM has no operations gets a one-step default template, so its orders are
  accepted right away. Each step's run time per unit is the operation's
  cycle time divided by the units one cycle makes, as Odoo 19 and later plan it: the
  work center's *Product Capacities* row for the product or its unit, else the
  BoM quantity (converted to the product's unit). Odoo 19 and 20 Community have no operation
  description, so steps get no instruction unless a module adds the
  operation ``note`` field back. Runs daily, or from Settings.
* **Work centers → workstations**: an operation whose work center has an
  *OpenMES Workstation Code* is sent with that ``workstation_code`` and the
  ``line_code`` of the product's OpenMES line (or the default line), so the
  planner no longer assigns workstations by hand; operations on work centers
  without a code are sent without a workstation, as before. Changing or
  removing a code creates a new routing version in OpenMES at the next sync
  (daily, or **Sync Products & BoMs**); unchanged routings are skipped by
  OpenMES. The code is at most 50 characters, without leading or trailing
  spaces, and unique within a company (a work center without a company
  clashes with every company); manufacturing managers can set it.
* **Stock**: on-hand quantities of those products and components are pushed
  hourly as a snapshot for every warehouse with an *OpenMES Warehouse Code*.
  Odoo is the source of truth for stock, so the OpenMES stock documents are
  always booked first (see below), and a warehouse that still has unbooked
  documents gets no snapshot (the sync log says why).
* **Material lots**: with the same hourly run (and **Sync Stock**), the lots
  of lot-tracked components (*Tracking: By Lots*) are sent to
  ``material-lots/import`` per warehouse with an *OpenMES Warehouse Code*,
  before that warehouse's snapshot (a lot import recomputes the OpenMES
  warehouse total from the lots; the snapshot then sets it to Odoo's
  on-hand). A lot's quantity is the sum of its quants in the warehouse stock
  location and its sub-locations; lots at zero or below are not sent. The
  lot's *Internal Reference* (``ref``) goes as ``supplier_lot_no`` and, when
  the optional *Product Expiry* module (``product_expiry``) is installed, its
  expiration date as ``expiry_date`` (the date in the time zone of the
  warehouse's address, else of the company's address, else UTC — never the
  user's, so the hourly run and the button send the same date). The
  quantity replaces the lot's balance in OpenMES, so a lot that ran out in
  Odoo is sent once more with 0 (without a unit): every lot sent with a
  quantity is remembered per warehouse (``openmes.material.lot.sent``) and
  forgotten after OpenMES accepted its 0. A refused or unanswered 0 is sent
  again by the next runs; after three refusals in a row it is given up with
  a line in the log (set the lot to 0 in OpenMES by hand). When OpenMES
  reports an error without a row number, every remembered lot is kept. A
  lot-tracked material that is no longer sent (it left every recipe or is
  no longer tracked by lots) gets its remembered lots set to 0 in OpenMES,
  even while Odoo still has them, since nothing would keep them current. Odoo deletes empty quants, so remembering is the only way
  to know which lots OpenMES still holds; resending every lot of a material
  with 0 would grow without bound. A warehouse blocked by unbooked stock
  documents gets neither lots nor snapshot, and no lots are sent when the
  stock documents could not be read. The outcome is one *Material lots*
  entry in the sync log per run; each line names the warehouse and
  ``material / lot`` (e.g. ``[0100] FLOUR-01 / L1: unit_of_measure: …``).
* **Stock documents**: material issues, product receipts and their reverse
  types posted in OpenMES are fetched every 15 minutes (``unsynced_only``),
  booked in Odoo as done stock moves without a picking — issues from the
  warehouse stock location to the product's production location, receipts the
  other way — and acknowledged with the moves' reference
  ``OpenMES <document no.>``. The warehouse is the Odoo warehouse whose
  *OpenMES Warehouse Code* equals the document's ``warehouse_code`` or
  ``warehouse_erp_code``; the product is found by *Internal Reference*; a lot
  is found or created for lot/serial tracked products. Each document is booked
  once (its OpenMES id is unique); a failed acknowledgement is only re-sent.
  Bookings are committed before any acknowledgement is sent, and only one run
  books at a time (a run that overlaps another's booking is skipped, together
  with its stock snapshot; acknowledgements may overlap, which is harmless).
  An acknowledgement answered with 404 (OpenMES does not know the document)
  is not retried automatically.
  A document of a work order made from a manufacturing order is not booked:
  the MO consumes the components and receives the product itself, so the
  document waits until the MO is done and is then only acknowledged with the
  MO's name (a cancelled MO's documents are booked as moves). An *OpenMES
  Warehouse Code* is unique within a company (another company, another
  OpenMES instance, may use the same code).
* **Production lines**: the OpenMES lines (``GET /api/v1/erp/lines``) are
  kept as a list in Odoo (**Sales > OpenMES > Production Lines**), so the
  default line in Settings and the line of a product are picked from it
  instead of typed. The list is refreshed daily and with **Refresh Lines** in
  Settings; it is always read in full, cursor by cursor: OpenMES does not list
  deleted lines, so a line missing from the list (deleted in OpenMES) or
  inactive there becomes inactive (archived) in Odoo, and one that comes back
  is reactivated. A failed or incomplete read changes nothing. Every line
  belongs to one company (the one whose OpenMES instance listed it), so two
  plants may both have a line ``L1``. The line code is still sent with every
  work order; the product field *OpenMES Line Code* stays available (computed
  from the line) for imports and RPC, and writing an unknown code to it is
  refused.
* **Settings per company**: the URL, API key, default line, order prefix,
  order source, the integration switch, the webhook and the pull markers
  belong to the company (**Settings > OpenMES** shows the current company's;
  switch company to configure another). A company with two plants on two
  OpenMES instances (or tenants) uses one Odoo company per plant. Every
  request goes to the instance of the record's company: a work order to its
  company's, and each cron runs once per company with the integration
  enabled. Master data and stock are per company: the products of that
  company plus the shared ones (cost and currency of that company), its BoMs
  and its warehouses. Stock documents, sync log entries and lines belong to
  the company whose instance they came from. One webhook URL serves every
  company: the delivery's signature tells which company's secret signed it,
  and it only changes that company's work orders. A run that fails for one
  company (any error, not only an OpenMES answer) is logged and rolled back for
  that company only; the others carry on. Every OpenMES setting of a company is
  readable and writable by administrators only. Changing a company's OpenMES
  URL or API key (another instance, or a key whose webhooks OpenMES
  deactivated) clears its pull markers, so the next pull reads the new
  instance from the start, and forgets its webhook (logged with the webhook
  id): delete it in the former OpenMES and register again.
* **Connection test with scopes**: **Test Connection** asks OpenMES what the
  API key may do (``GET /api/v1/erp/me``) and shows the key's name, tenant,
  OpenMES version and expiry, every scope the module uses that the key lacks
  (with the features that will fail without it) and whether the *Warehouses*
  module is enabled. A missing scope is a warning, not an error. *Warehouses*
  disabled is only a warning when a warehouse has an *OpenMES Warehouse Code*.

Configuration
=============

From scratch, for one company; repeat steps 3–8 for every further company.

1. **OpenMES**: enable the *ERP integration* module, and *Warehouses* for
   stock and stock documents.
2. **API key**: in OpenMES create an API key under **Settings > API Keys**.
   Give it the scopes of the features you use (**Test Connection** lists the
   missing ones with what will fail):

   ========================== ================================================
   Scope                      Feature
   ========================== ================================================
   ``erp:orders:import``      send work orders, changes and cancellations
   ``erp:production:read``    status pull, change requests, settlements
   ``erp:masterdata:read``    production line list
   ``erp:masterdata:write``   products, routings, materials, BoMs, material
                              lots (without it the lots fail in their own
                              log entry; the stock snapshot is still sent)
   ``erp:stock:read``         stock documents (without it no stock snapshot
                              is sent at all: it could overwrite unbooked
                              consumption)
   ``erp:stock:write``        stock snapshot, stock document acknowledgement
   ``erp:webhooks:manage``    register and unregister the webhook
   ========================== ================================================

3. **Odoo settings** (**Settings > OpenMES**, for the current company): enter
   the URL and the API key, click **Test Connection**, save, click **Refresh
   Lines**, pick the default line and set the work order prefix (a different
   prefix per company is advised). Two enabled companies cannot share one URL
   and key.
4. **Webhook** (optional, recommended): set ``web.base.url`` to the address
   OpenMES reaches Odoo at and freeze it (see *Network requirements* below),
   save, then click **Register Webhook** in **Settings > OpenMES > Webhooks**.
   Odoo registers ``web.base.url`` + ``/openmes/webhook`` with the five events
   and stores the signing secret OpenMES returns (it is shown nowhere).
   **Unregister Webhook** deletes it in OpenMES and returns the status pull to
   15 minutes. The block shows the webhook of the company the Settings are
   open for: *Registered* with its id and URL, or *Not registered* with the
   URL that would be registered; both buttons reload the Settings, so the
   status shown is always the stored one. *Incomplete* means Odoo knows a
   webhook id but not its signing secret (deliveries cannot be verified):
   click **Unregister Webhook**, then register again.
5. **Warehouses** (**Inventory > Configuration > Warehouses**): give each
   warehouse that exists in OpenMES its *OpenMES Warehouse Code* (the code or
   ERP code of the OpenMES warehouse), unique within the company. Warehouses
   without a code get no stock snapshot and no stock documents.
6. **Work centers** (**Manufacturing > Configuration > Work Centers**,
   needs *Work Orders* enabled in the Manufacturing settings): give each work
   center that matches an OpenMES workstation its *OpenMES Workstation Code*
   (the ``code`` of the workstation in OpenMES). The workstation must be on
   the OpenMES line the product's work orders go to (its *OpenMES Line* or the
   default line), because that line is sent with it. Work centers without a
   code are fine: their operations get no workstation.
7. **Work order source**: choose *Create OpenMES work orders from* — *Sale
   order lines* or *Manufacturing orders* (see Features). Then, on each
   product to manufacture: set an *Internal Reference* (it is the OpenMES
   product type code), open the *OpenMES* tab and tick *Manufacture in
   OpenMES*; optionally pick an *OpenMES Line* (without one the default line
   is used).
8. **Enable** the integration and save, then run **Sync Products & BoMs**
   once (products with BoM operations get their routing from Odoo, the others
   a one-step default template you can refine in OpenMES) and **Book Stock
   Documents** / **Sync Stock**.
9. **Each further company** (plant on another OpenMES instance or tenant):
   switch to it and repeat steps 2–8 with its own URL and its own API key.

Network requirements for webhooks
---------------------------------

* OpenMES must be able to reach Odoo at ``web.base.url``. Set it to the
  address OpenMES uses (not ``localhost``) and freeze it with the system
  parameter ``web.base.url.freeze`` = ``True``, otherwise an administrator's
  login can change it. Use https in production; the settings warn when the
  address is not https.
* OpenMES only delivers to public addresses by default. For Odoo in the local
  network (e.g. ``http://192.168.1.50:8069``) an OpenMES administrator must
  add it under **Webhooks > Endpoints > Local network receivers** (a CIDR
  range such as ``192.168.1.0/24``, a single address or an exact hostname),
  otherwise registration fails with *Webhook URL resolves to a
  private/reserved address*. Loopback (``127.0.0.1``) is always blocked, so
  OpenMES and Odoo on the same host need a non-loopback address.
* With several databases on one Odoo server, ``/openmes/webhook`` must
  resolve to one database (``dbfilter`` or a single database per host name).
* A reverse proxy must pass the body unchanged and keep the ``X-OpenMES-*``
  headers. The endpoint is public (authenticated only by the signature);
  consider a rate limit on it in the proxy.

Usage
=====

Work orders are listed under **Sales > OpenMES > Work Orders** and on the
*OpenMES* smart button of the sale order or manufacturing order. An order in
*Error* shows the reason (for example an unknown line code). Fix it and click
**Send to OpenMES**; failed orders are also retried automatically up to five
times.

Order numbers are ``PREFIX-SALEORDER-LINEID`` (sale order lines) or
``PREFIX-<MO name>`` with ``/`` replaced by ``-``, e.g. ``ODOO-WH-MO-00012``
(manufacturing orders), and orders are sent with the
``update_or_create`` strategy, so a resend never duplicates an order. Each
order also carries its source (``odoo``, ``sale.order.line,<id>`` or
``mrp.production,<id>``), the customer and, for sale order lines, the
price. An order waiting for a manager's decision in OpenMES shows the change
request and its status on the work order form.

The filter **Needs Attention** lists the work orders with a quantity
difference. Adjust the sale order or MO in Odoo (that resends the quantity),
or — as a sales or manufacturing manager — click **Accept OpenMES Quantity**
on the work order: it sets the work order's planned quantity to the quantity
OpenMES plans (or produced, once done) and clears the flag. It sends nothing
to OpenMES and does not change the sale order, the MO, deliveries or
invoices; the activity stays open until the person marks it done.

The *Settlement* tab of a work order done in OpenMES shows its scrap, cost
and material consumption, or the reason the fetch failed (filter
**Settlement Failed**). A sales or manufacturing manager can click **Fetch
Settlement** to fetch it at once — after five failed attempts, or for an order
that was done before the module could fetch settlements. What the numbers
mean:

* *Scrap*: the total of the order's scrap entries in OpenMES, in the unit of
  the work order's product.
* *Total Cost*: the OpenMES *Production Cost* report — materials, labour
  (logged work time × pay rates) and manually booked additional costs.
  Recorded consumption is priced as recorded; a material with no recorded
  consumption is estimated from the BOM × produced quantity. OpenMES has no
  machine cost, so none is included.
* *Cost per Unit*: total cost ÷ produced quantity (0 when nothing was
  produced).
* *Material Cost*, *Labour Cost*, *Additional Costs*: the three parts of the
  total cost (``costs.materials.total``, ``costs.labor.total``,
  ``costs.additional.total``).
* *Cost Currency*: the code OpenMES sent, shown as is. The amounts are never
  converted to the company currency; *Cost Currency in Odoo* is only filled
  when Odoo knows the code. *Mixed Currencies* means some cost items were in
  another currency and OpenMES summed them without conversion.
* *Material Consumption*: one line per material allocation in OpenMES with
  its consumed, scrapped and returned quantity in the OpenMES unit (not
  converted). *Lots* lists the lots picked for it with the **picked**
  quantity, which can differ from the consumed one. *Product* is the Odoo
  product whose Internal Reference equals the material code, when exactly one
  does.

A routing that OpenMES rejects shows in the *Routings* sync log (**Sales >
OpenMES > Sync Log**) with the product code, the step and OpenMES's message,
followed by the Odoo work center, e.g. ``BREAD-01: steps.1.workstation_code:
Workstation 'WS-OVEN' not found (Odoo work center: Oven)``. Create the
workstation in OpenMES (on the product's line) or correct the work center's
*OpenMES Workstation Code*; the whole routing is retried by the next sync.

**OpenMES costs in the MO cost.** Switch on *Add OpenMES labour and
additional costs to the manufacturing order cost* in **Settings > OpenMES >
Work Orders** (for the company of the Settings form; admins only). From then
on, every MO with an OpenMES work order that is closed — automatically when
OpenMES reports it *Done*, or by hand — gets:

* **Added**: the OpenMES labour (``labor.total``: logged work time × pay
  rates) and additional costs (``additional.total``: manually booked costs),
  as the MO's *Extra Unit Cost* = (labour + additional) ÷ the quantity being
  closed, in the product's unit of measure. Odoo multiplies it back by that
  quantity, so the whole OpenMES amount is booked on the finished product. An
  *Extra Unit Cost* entered by hand is replaced (the note names the old one).
* **Not added**: OpenMES material costs (Odoo values the consumed components
  from its own stock valuation — adding them would count them twice) and
  machine cost (OpenMES has none).
* **Not set, with a chatter note saying why**: the settlement is in another
  currency than the company's (or an unknown one), OpenMES reports
  *Mixed Currencies*, the settlement cannot be fetched (OpenMES error, time
  out, invalid data, an OpenMES without settlements), or the work order is
  not *Done* in OpenMES yet when the MO is closed by hand. The MO is closed
  anyway.

When the stock valuation uses it: the *Extra Unit Cost* enters the unit cost
of the finished product only when its product category's costing method is
*FIFO* or *Average Cost (AVCO)*; with *Standard Price* the finished goods stay
at the standard price. With *Automated* inventory valuation the value is also
posted to the stock accounts in a journal entry; with *Manual* valuation it
only changes the stock valuation layers.

Stock documents are listed under **Sales > OpenMES > Stock Documents** and
**Manufacturing > Manufacturing > OpenMES Stock Documents** (inventory and
manufacturing users can read them). The default filter *Not Booked* shows
documents in *Error* (unknown product, warehouse or lot, a unit that differs
from the product's unit, a tracked product without a lot…), *Waiting for MO*
and *Pending*; each of them blocks the stock snapshot of its warehouse. A
document in error stays on the OpenMES backlog: fix the cause and it is booked
by the next run, or click **Retry** (inventory or manufacturing managers).
**Retry** reads the OpenMES backlog of the document's company again first: a
document still on it is booked with its current lines, one that is gone from
it is not booked (see below), and when the backlog cannot be read nothing is
booked (the booked documents of the selection still get their acknowledgement
re-sent). If you booked it by hand, **Acknowledge Without Booking** takes it
off the backlog and releases the snapshot: the document becomes *Booked* with
the handling *Not booked* before the acknowledgement is sent, so if OpenMES
does not answer, only the acknowledgement is re-sent later — the document is
never booked by Odoo. *Booked* and *Acknowledged* documents cannot be deleted
(not even by an administrator): their record is what keeps a document still
listed by OpenMES from being booked a second time. A document that leaves
the OpenMES backlog before Odoo booked it (acknowledged in OpenMES by someone
else) is not booked; it is marked *Acknowledged* with the handling
*Acknowledged outside Odoo*. *Booked* means the moves are done but the
acknowledgement failed; it is re-sent automatically up to five times, then
only with **Retry**. **Settings > OpenMES > Book Stock Documents** runs the
import at once.

Upgrading from Odoo 19 to Odoo 20
=================================

Upgrade the database from Odoo 19 to 20 with Odoo's standard migration tool
(the Odoo Upgrade service, or OpenUpgrade for Community), with the 19.0 module
installed; then install this 20.0 module in place of the 19.0 one. The module
has no cross-version migrations and the data model is the same: no field is
added or removed and the unique constraints keep their names. Odoo 20 merges
access rights and record rules into *Accesses* (``ir.access``): the module's
access rights and multi-company rules keep their XML ids
(``access_openmes_*``, ``openmes_*_company_rule``), so Odoo's migration
replaces the old records in place.

Upgrading from Odoo 18 to Odoo 19
=================================

The module has no cross-version migrations. Upgrade the database from Odoo
18 to 19 with Odoo's standard migration tool (the Odoo Upgrade service, or
OpenUpgrade for Community), with the 18.0 module installed at its latest
version (18.0.1.1.15 or later); then install this 19.0 module in place of the
18.0 one. The data model is the same: the unique constraints keep their
names, and no field is added or removed. A new Odoo 19 database starts clean.

Upgrading from 18.0.1.1.0
=========================

This section is about the 18.0 module and is kept for reference: upgrade to
the latest 18.0 version first, then to Odoo 19 and Odoo 20 (above).

Update the module (``-u openmes_connector``); the data migrations run on
their own and keep every existing work order with its state, OpenMES id,
quantities, pending cancellation and change request. What changes:

* The global settings (``ir.config_parameter``: switch, URL, API key, default
  line, prefix, the ``since`` markers and a registered webhook) move to the
  **main company** and are deleted from the system parameters, even when
  OdooBot works in another company. A main company that already has an
  OpenMES URL keeps its settings; a webhook registered with the former
  settings is then logged as a warning — delete it in OpenMES. Invalid legacy
  values are imported with the integration disabled, so they can be
  corrected in Settings.
* Every typed line code (products and the default line) becomes a line in
  the main company, named after its code; the next refresh fixes the name or
  archives codes OpenMES does not know. A product of another company keeps no
  line and uses its company's default line. Existing stock documents get
  their warehouse's company, else the main company.
* Existing work orders become *Sale order lines* work orders of their sale
  order's company and customer; the source mode stays *Sale order lines*.
* Orders already done in OpenMES with another quantity than planned are
  flagged with a *Quantity Difference* but get no activity; those done before
  the upgrade get no settlement until someone clicks **Fetch Settlement**.
* The new crons (settlements, stock documents, line refresh, webhook events)
  are created; the existing ones keep their schedule.
* From 18.0.1.1.8 or earlier: the webhook delivery log becomes the webhook
  event queue. Deliveries of the last 24 hours are kept as processed events,
  so an OpenMES retry across the upgrade is still answered as a duplicate.

``scripts/test_upgrade.sh`` in the repository checks all of this on a
database created with 18.0.1.1.0 (it runs on Odoo 18.0 only).

Known limitations
=================

* Requires an OpenMES version with the ERP change-request API (work-order
  cancel, ``results`` in the import report, the change-request feed).
  OpenMES versions without ``GET /api/v1/erp/me`` cannot report scopes:
  **Test Connection** then only checks that the ERP API answers (warning).
* Production lines: the refresh reads the whole list every time (``since``
  is not used, because a ``since`` read cannot see deleted lines); OpenMES
  line lists are small. A line whose code is changed in OpenMES shows up as a
  new line while the old one is archived; products keep the archived line
  until someone picks the new one. A product with an archived line still sends
  its code, and OpenMES then reports the unknown line on the work order (no
  silent switch to the default line). A product shared by several companies
  has one *OpenMES Line*: its orders use it only in the line's company, the
  other companies use their default line.
* Work centers: the code is one plain field, so a work center shared by
  several companies (no company) has one *OpenMES Workstation Code* for all
  of them. A routing carries one line for all its steps (the product's line or
  the default line); a line code longer than 50 characters is not sent (the
  workstation is then looked up without it). A BoM shared by all companies
  that uses another company's work center sends that step without a
  workstation (the code belongs to the other company's OpenMES). Saving a code
  sends nothing: the new routing version is created by the next master data
  sync.
* Webhooks: while one is registered, change requests (approved / rejected)
  are also only polled hourly; OpenMES sends no event for them. The legacy
  signature without a timestamp is not accepted. Uninstalling the module does
  not delete the webhook in OpenMES: unregister it first. After a new URL or
  API key Odoo forgets the webhook; delete it in the former OpenMES and
  register again. Events are applied by a cron, so a change shows in Odoo a
  few seconds after OpenMES sent it (longer while the cron workers are busy).
  Events wait while their work order is being sent (retried every 30
  seconds, without a limit) and later events wait behind them, so one order
  in arrival order is never overtaken. A send that meets a webhook event on
  the same work order skips it and sends it again within a minute (the
  ``update_or_create`` import makes the resend harmless); **Send** then shows
  a warning instead of an error.
* Stock documents: units are not converted — a line must be in the product's
  unit (spelling variants such as ``pcs``/``Units``/``szt`` or ``kg``/``KGM``
  are accepted); a line without a unit is an error. Quantities are rounded to
  the *Product Unit* decimal precision that Odoo uses for every unit (e.g.
  1.2345 kg is booked as 1.23 kg with the default 2 digits), like everywhere
  else in Odoo. Issues
  are booked from the warehouse stock location without reservation and may
  drive on-hand stock negative.
* Material lots: the expiry date is only read from ``product_expiry``; the
  module's test suite runs without that module, so the real field is
  exercised only by a stand-in in tests. Only lot-tracked BoM components
  are sent (finished products and serial-tracked components are not). A lot's quantity is in the
  material's unit, so OpenMES refuses it when its material has another unit
  there. The remembered lots are written in the same transaction as the
  run: **Sync Stock** commits at its end, the hourly run once per company
  after all its warehouses. If that transaction is rolled back after
  OpenMES accepted lots (e.g. an unexpected error in a later warehouse of
  the same company), the remembered lots are lost with it and a lot that
  runs out later may stay in OpenMES with its last quantity until it is
  corrected there. Removing a warehouse's
  *OpenMES Warehouse Code* stops its lots, including pending zeros.
* Settings per company: two companies on the same OpenMES tenant need two API
  keys and must not use the same OpenMES warehouse codes, or both would fetch
  and book the same stock documents (Odoo cannot tell that two keys belong to
  one tenant). The status pull runs hourly only when every enabled company
  has a webhook. Only one stock-document run books at a time across all
  companies; a company skipped because another was booking is retried by the
  next run.
* Quantity differences: nothing is corrected automatically in sale orders,
  MOs, deliveries or invoices. No activity is created when the order has no
  responsible user who can read it (none set, archived, portal, or of another
  company); the work order is still flagged and the chatter message posted.
  A rejected cancellation creates an activity but no flag. After **Accept
  OpenMES Quantity** on a sale order line work order, a later change of that
  line (even its description) resends the line's quantity and can open a new
  change request. Quantities are compared at the precision of the *Product
  Unit* decimal setting (Odoo 20 units have no rounding of their own).
* Manufacturing orders mode: work orders carry no unit price. Marking an MO
  done (or partly done) by hand in Odoo does not finish the OpenMES order.
  Finished products tracked by lots get a lot from Odoo, but OpenMES never
  receives that lot number. Finished products tracked by serial numbers are
  never closed automatically: assign the serial numbers and mark the MO done
  in Odoo. Marking the MO done clears the work order's last error, but the
  To-Do stays open until its assignee marks it done. Lot and serial numbers of
  components and by-products are not assigned automatically, so a tracked
  component makes the automatic close fail until someone sets them and
  clicks **Close Manufacturing Order**; a close that needs a person's
  confirmation in Odoo (e.g. a strict BoM consumption warning) fails the same
  way, and until then the work order shows the quantity difference. The MO
  consumes its BoM quantities, not what OpenMES reported, and the stock
  documents of an MO that is not done yet block its warehouse's snapshot.
  Cancelling a sale order does not cancel the MTO manufacturing order made
  for it (standard Odoo), so its OpenMES work order keeps running until the
  MO itself is cancelled. A flagged product sold from stock (no MO) never
  reaches OpenMES in this mode.
* Settlement: fetched once per work order — consumption or costs recorded in
  OpenMES after the order was done are not picked up. Unless the MO cost
  option is on (below), the cost is not booked in accounting and does not
  change the product cost or the MO valuation. It is never converted between
  currencies. Consumption booked against a lot without
  a material allocation in OpenMES is not included, and at most 1000
  consumption lines are kept. Requires an OpenMES version that supports
  ``include`` on ``GET /work-orders/{id}``; with an older one the fetch fails
  with a message saying so.
* OpenMES costs in the MO cost: set only at the close, because Odoo values
  the finished product once, when the MO is marked done, and has no way to
  revalue it consistently afterwards. An MO closed without them (fetch
  failed, other or mixed currency, option switched on later, MO closed by
  hand before OpenMES finished) is not corrected later, even when the cron
  fetches the settlement afterwards. Needs Odoo's *Manufacturing* cost
  accounting (``mrp_account``, installed automatically with stock
  accounting); without it the option does nothing (the settings say so).
  The settlement fetch runs inside the close: up to 20 seconds per MO, also
  for a close by hand. Odoo also adds the cost of time logged on Odoo work
  orders (work centre cost per hour): logging that time as well counts the
  labour twice — leave it out or set the work centre cost to 0. Only one
  finished OpenMES work order per MO is used; with several none is added. An
  MO with by-products shares the added cost with them as Odoo's cost share
  says.

Support
=======

Questions, bug reports and feature requests: contact@getopenmes.com.
OpenMES: https://getopenmes.com. Source code: https://github.com/Mes-Open.
