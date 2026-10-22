# Outbound notifications

Every place the platform calls a caller-registered destination (`PR-MSG-1.1`). They all go through `smo_shared/webhook.py` (the SSRF
guard) today; the ones that are pure notifications move to the transactional outbox (`smo_shared/outbox.py`) one module at a time
(`MSG-1.5`–`1.9`, `OPEN_ITEMS.md` §5.3). `tests_integration/test_notification_inventory.py` fails when a call site is added or moved
without a row here.

## Classes

| Class | Meaning | Where it goes |
|---|---|---|
| **A** | A POST whose answer nothing reads: a notification. A lost one means a subscriber never learns of a change | Moves to the outbox (the PR named in the last column) |
| **B** | A command to a destination (a DELETE) whose answer nothing reads | Moves to the outbox as a `DELETE` row (`method` column, `PR-MSG-1.10`); there is one such site, and it has moved |
| **C** | A read whose answer the caller uses to decide something (a health probe, a capability discovery) | Stays inline, always: the caller needs the answer in the request |

Everything classed A ignored the response (`post_webhook` returns it and no caller kept it), including the Intent Service's
RMIH callback, so no A-class site needed the response: the first sort of site the plan worried about did not occur. All of them have
moved (`PR-MSG-1.5`–`1.9`); `PR-MSG-1.10` moved the one class-B DELETE as a `DELETE` row; what is left inline is the two class-C reads.
After the move, a notification is sent after the transaction that caused it commits, never before, and a rolled-back change sends nothing.

## Not a notification: the gateway's forward to a rApp's operator API

`smo_shared.webhook.forward_to_destination` (`PR-GUI-8`) is a fourth helper beside `post_webhook`, `get_webhook` and `delete_webhook`, used by R1 Termination's `_forward_operator_api` only. The destination is a base URL a rApp instance registered (`rapp_instance.operator_api_base`), so it gets the same guard (`is_safe_webhook_destination`, again before every call) and, for an https destination inside the deployment, the same client certificate. It is not a row of the table below because it is not a notification: the caller of the gateway waits for the answer, nothing is stored or retried, and an unreachable rApp is a 502 to that caller, not a lost message. The inventory test lists `post_webhook`, `get_webhook`, `delete_webhook` and `enqueue` only, so a new use of any of those still needs a row.

## Call sites

| File | Function | Helper | Class | What it tells whom | Moves in |
|---|---|---|---|---|---|
| `dme/app/main.py` | `_notify_type_subscribers` | `enqueue` | A | Every type subscriber: a type was created, changed or removed | moved (MSG-1.5) |
| `dme/app/main.py` | `terminate_data_offer` | `enqueue` | A | The offer owner's termination URI: the offer was removed | moved (MSG-1.5) |
| `dme/app/main.py` | `_push_job_to_producers` | `enqueue` | A | Each supporting producer's job callback: a data job exists | moved (MSG-1.5) |
| `dme/app/main.py` | `_stop_job_at_producers` | `enqueue` | A | Each supporting producer: stop this job (a DELETE row) | moved (MSG-1.10) |
| `dme/app/main.py` | `_send_open_jobs` | `enqueue` | A | A producer that joined a type or moved its job callback: each open job of the type (call flow 11a) | moved (DOC-CF-1.8 flow 11, outbox from the start) |
| `dme/app/main.py` | `deregister_producer` | `enqueue` | A | The leaving producer: stop each of its jobs (a DELETE row, call flow 11d) | moved (DOC-CF-1.8 flow 11, outbox from the start) |
| `dme/app/main.py` | `delete_dme_type` | `enqueue` | A | Each offer owner's termination URI: the offer went with its deleted type (call flow 11d) | moved (DOC-CF-1.8 flow 11, outbox from the start) |
| `dme/app/main.py` | `_producer_is_healthy` | `get_webhook` | C | A producer's health URL: answers the type's ENABLED/DISABLED status | stays inline |
| `mock-o1-adaptor/app/main.py` | `_emit` | `post_webhook` | A | RAN NF OAM (the stub's configured or requested target): a test trigger of the stub; its answer is read by the caller of the stub | stays inline (exempt) |
| `sme/app/main.py` | `_deliver` | `enqueue` | A | Event subscribers: a service API became available, changed or went away | moved (MSG-1.6) |
| `aimgf/app/main.py` | `_notify_job_completion` | `enqueue` | A | The job's notification URI: a training or inference job finished | moved (MSG-1.7) |
| `aimgf/app/main.py` | `report_performance` | `enqueue` | A | A model subscriber: a performance report, possibly below its floor | moved (MSG-1.7) |
| `focom/app/main.py` | `_notify_inventory_subscribers` | `enqueue` | A | Inventory subscribers: a resource changed | moved (MSG-1.8) |
| `focom/app/fcaps.py` | `_notify` | `enqueue` | A | Alarm subscribers: an alarm was raised, changed or cleared | moved (MSG-1.8) |
| `focom/app/fcaps.py` | `_report` | `enqueue` | A | Performance subscribers: new measurements | moved (MSG-1.8) |
| `mdaf/app/main.py` | `_notify_report_subscribers` | `enqueue` | A | Analytics subscribers: a report crossed their threshold | moved (MSG-1.8) |
| `mdaf/app/mda.py` | `_deliver` | `enqueue` | A | An MDA request's reporting target: a report, or a report file, is ready (two call sites). `delivery.notified` is set whatever the answer was; after the move it means "enqueued" | moved (MSG-1.8) |
| `intent-service/app/main.py` | `_create_intent_row` | `enqueue` | A | The RMIH's notification destination: an intent was created | moved (MSG-1.9) |
| `intent-service/app/main.py` | `_deliver_report` | `enqueue` | A | Each report recipient of an intent: a report is available | moved (MSG-1.9) |
| `intent-service/app/main.py` | `_notify_autonomy_operator` | `enqueue` | A | The operator's destination: an autonomy dispatch outcome | moved (MSG-1.9) |
| `ran-nf-oam/app/main.py` | `_refuse` | `enqueue` | A | Safeguard subscribers: an rApp's request was refused (one per subscriber, in the same commit as the refusal record; throttled per invoker and code) | moved (AI-10.6) |
| `ran-nf-oam/app/main.py` | `_notify_approvers` | `enqueue` | A | Approval subscribers (the approvers): an rApp action waits for a decision, or a request lapsed with nobody deciding (one per subscriber, in the transaction that parks or lapses the request). A request whose policy asks for two approvals adds `requiredApprovals` and `approvalsGiven` to the payload; every other notice is unchanged. There is no notice for the first of two approvals (the second approver was told with `RAPP_APPROVAL_REQUESTED` and the inbox shows the count) | moved (AI-11.5) |
| `ran-nf-oam/app/main.py` | `report_pm_file` | `enqueue` | A | File subscribers: a PM file is ready (one per subscriber, after the commit already) | moved (MSG-1.9) |
| `ran-nf-oam/app/lifecycle.py` | `_notify_lifecycle` | `enqueue` | A | Lifecycle subscribers (`/lifecycle-subscriptions`, admin-registered; narrowed by event type): an element's onboarding ended FAILED (`ONBOARDING_FAILED`), a software campaign halted for a failed gate or by an operator, never for the routine pause between waves (`CAMPAIGN_HALTED`), or a rollback ended with a revert job failed (`CAMPAIGN_ROLLBACK_FAILED`). One per subscriber that wants the event, in the transaction that records the failure or the halt; the one call site serves all three events | in the outbox from the start (MGT-14.7, MGT-15.6) |
| `ran-nf-oam/app/vendors.py` | `onboard_vendor` | `get_webhook` | C | The adaptor's `/capabilities`: the answer fills the vendor profile | stays inline |

SA SMOS has no call site: its notifications, where it has any, travel through R1 Termination, not to a registered destination.
