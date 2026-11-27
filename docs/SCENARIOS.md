# Production scenarios: how the memory layer behaves when things get real

The baseline demo shows the happy path: one customer, three channels, one memory. This guide covers the
questions a production review asks next. Each section names the problem, what the PoC does, where the code is,
and what changes for production. Every scenario is a one-click run in the demo console
(`FAKE_MODEL=1 BACKEND=local uvicorn cx_memory.api:app --port 8080`) and has a test in `tests/test_scenarios.py`.

## Where the memory layer sits

```
 Channels / contact center                     Systems of record (cards, servicing, core, CRM)
   web chat · voice (IVR, ANI) · email                   │ case/dispute updated events
            │                                            ▼
            ▼                              ┌──────────── event stream (Kafka stand-in) ───────────┐
  ┌─ OmnichannelGateway ─────────────┐     │ sor.case.updated.v1   cx.interaction.v1              │
  │ identity + assurance + step-up   │◄────┤ crm.activity.v1       cx.memory.lifecycle.v1         │
  │ token vault (HMAC)               │────►│ (partition key = customer token, no raw text)        │
  │ partition router                 │     └──────────────────────────────────────────────────────┘
  │ ADK channel agents               │                         │ consumers: personalization,
  └──────────────┬───────────────────┘                         │ servicing analytics, data lake
                 ▼                                             ▼
  ┌─ GovernedMemoryService (one choke point) ─────────────────────────────────────────┐
  │ consent · assurance · redaction · special categories · ledger (TTL, supersede)    │
  │ quotas per partition · extraction policy · breaker · timeout · outbox · audit      │
  └──────────────┬───────────────────────────────────────────────┬───────────────────┘
                 ▼                                               ▼
     Tier 1 live context (seconds)                  Tier 2 Memory Bank, one scope per partition
     Memorystore / Firestore in prod                (app_name = cx_unified_<partition>, user_id = token)
```

Principles that run through every scenario:

1. **The system of record stays the source of truth.** The profile is read live on every turn. Memory holds
   conversational context and carries provenance, so an SOR event can supersede it.
2. **The memory layer is not a new pipeline.** It consumes certified business events and publishes one
   standardized, taxonomy-coded interaction event per turn. Downstream consumers (personalization, servicing,
   marketing, the data lake) subscribe to the stream; they don't read the memory store.
3. **Every read and write goes through one governed choke point.** Agents, tools, the CRM connector and ops
   all use `GovernedMemoryService`, so policy can't be bypassed by a new caller.

---

## 1. Customer calls from a different phone

| | |
|---|---|
| Problem | Caller number (ANI) is the voice identifier. A new phone, a borrowed phone or a spoofed number must not unlock someone's memory. |
| PoC | Unknown number → **no model call, no profile, no memory read**; the IVR asks for the account number and sends a one-time passcode to the phone **on file** (never to the calling phone). On success the call continues at `HIGH` assurance and the number is linked as a **new device for 30 days**. 3 wrong codes → locked, route to fraud. |
| Assurance levels | `HIGH` authenticated web session or passed step-up · `MEDIUM` known caller number / sender address (spoofable) · `NONE` unknown or shared. |
| ATO protection | Contact-detail changes (new phone, email, address) need `HIGH`. On a `MEDIUM` voice call the write is refused with `needs_step_up` and the agent offers verification. |
| Code | `cx_memory/identity.py` (`resolve_contact`, `StepUpService`), `cx_memory/governance.py` (`CONTACT_CHANGE`), `gateway._step_up_reply` |
| Production | ANI plus carrier risk / STIR-SHAKEN attestation from telephony; OTP via the enterprise authentication service; linked-device registry in the identity platform; voice biometrics as an optional factor. |

**Shared household line.** One landline, two customers (Maya and Arjun). An ambiguous identifier is treated like
an unknown one until the caller picks their account and verifies, which closes a common cross-customer disclosure path.

## 2. Contact center and CRM integration

| | |
|---|---|
| Problem | Memory that only bots can see doesn't help. The human agent needs the same context in the CRM desktop, and CRM must stay the record for cases. |
| Inbound | `POST /connect/contact` takes a contact-flow event (Amazon Connect shape: `ContactId`, `Channel`, `CustomerEndpoint.Address`, `Attributes.intent`). It returns **contact attributes** for the flow (token, assurance, queue, priority; no PII) and a **screen pop**: open issues from the SOR, memory highlights with provenance (source, age, expiry), recent activity on other channels, next-best actions. |
| Routing | Accessibility needs → accessibility-trained queue; open dispute + dispute intent → disputes queue; segment desks. |
| Wrap-up | `POST /connect/contact/{id}/wrap-up` logs a CRM activity (disposition, taxonomy code, **redacted** notes), turns the agent's notes into a governed memory (`source=crm_note`), and publishes `crm.activity.v1` + `cx.interaction.v1`. |
| SOR events | `POST /sor/events` (e.g. dispute resolved) → `sor.case.updated.v1` → consumer supersedes the memory with the same reference (v1 `SUPERSEDED` → v2 `ACTIVE`) and deletes the stale copy from Memory Bank. Next turn on any channel answers with the new status. |
| Code | `cx_memory/crm.py`, `gateway._on_sor_event`, `cx_memory/events.py` |
| Production | Contact flow invokes a Lambda / Cloud Run endpoint; CRM write-back through the CRM API behind the outbox; SOR events from the certified business-event stream, consumers idempotent on `event_id`. |

## 3. Extraction cost and throughput

Extraction is an LLM call that Memory Bank makes on each write: new events + topic definitions + few-shot
examples + the customer's related memories (for consolidation). It's the biggest memory cost and the tightest quota.

| Policy | When it extracts | Trade-off |
|---|---|---|
| `per_turn` | every turn | freshest, most calls |
| `salient` (default) | only turns with memory-worthy signals: dates, money, reference IDs, instructions, contact/travel/issue words | skips small talk; same freshness for what matters |
| `batched` | once per contact at session end (if anything was salient), safety flush every N turns | fewest calls, more lag |

Tier 1 (live context) covers the lag, so the cross-channel handoff doesn't depend on extraction speed.

* **Per-turn cost** appears in every turn trace (model tokens, retrieve calls, extraction calls, estimated $).
* **Capacity plan** `GET /ops/capacity`: monthly calls, tokens, cost, and peak QPM vs the partition quota.
* **Burst test** `POST /ops/burst` / `cx_memory/loadgen.py`: N concurrent contacts against an isolated store with
  injected latency; reports turns/s, p50/p95, extractions, skips, throttles.

All prices are **illustrative placeholders** (`PRICE_*` env vars). Replace them with your rate card before you quote a number.

## 4. Partitions and quotas

| | |
|---|---|
| Partition | An isolated memory domain: its own scope (`app_name`), region, quota budget, per-customer caps and cost line. PoC: `consumer` (Retail, Private Client) and `business` (Small Business). |
| Why | Blast radius (a consumer spike can't starve business banking), data residency / LOB separation (own Agent Engine instance, region, IAM), cost attribution. |
| Quotas | Client-side token buckets per partition for read / write / extract, kept below the project's Memory Bank QPM. When a bucket is empty, reads fall back to Tier 1 + profile and writes go to the **outbox** rather than being dropped. |
| Per-customer | Max active memories (lowest-priority, oldest evicted first) and max writes per customer per day (abuse protection). |
| Code | `cx_memory/quotas.py`, used in `governed_memory.py` |
| Production | One Agent Engine per partition and region; quota increases sized from the capacity plan; labels for billing export. |

## 5. Tokenized customer identity

Memory Bank, sessions, the event bus and the audit log never see the internal customer ID. They see
`ctk1_<HMAC(partition | customer_id)>`:

* deterministic, so all channels land in the same scope;
* not reversible without the key, so a leaked export can't be joined back to a customer;
* partition-scoped, so the same customer has unrelated tokens in different partitions;
* versioned (`ctk1_`), so the key can be rotated.

Only the gateway's vault detokenizes, and each call is audited with actor and purpose (e.g. DSAR export).
The CRM name line is deliberately **not** copied into memory: names and identifiers come from the live profile.
Code: `cx_memory/tokenization.py`. Production: key in Cloud KMS / HSM or the enterprise tokenization service.

## 6. Memory lifecycle

| Topic | Retention |
|---|---|
| `travel_and_life_events` | last date in the fact + 14 days (default 180) |
| `open_service_issues` | 180 days; **30 days after** resolved/closed |
| `key_conversation_details` | 90 days |
| `user_personal_info` | 365 days, re-verify |
| `user_preferences`, `explicit_instructions`, `contact_and_accessibility_needs` | until changed or revoked |
| `product_holdings` | 30 days; SOR-owned (agents can't write it) |

The **ledger** records every governed memory: id, topic, source (agent tool, extraction, SOR seed, SOR event,
CRM note), classification, created/expiry, status, version, supersedes. States: `ACTIVE` → `SUPERSEDED` (same
subject written again, e.g. same dispute ID) · `EXPIRED` (sweeper) · `ERASED` (right to erasure) · `EVICTED` (cap).
Reads filter out anything not `ACTIVE`, which also guards against eventual consistency in the bank.

On Vertex, explicit facts are created with a per-memory `ttl` (native expiry) and `memory_id` = ledger id;
generated memories fall under the engine `ttl_config` in `scripts/setup_memory_bank.py`.
The demo clock (`POST /ops/clock/advance`) shows expiry without waiting days.
Code: `cx_memory/lifecycle.py`.

## 7. Governance and compliance

Every write is evaluated in this order (`governance.evaluate_write`):

1. **Consent:** withdrawn → session-only; withdrawal also erases stored memory.
2. **Assurance:** topic minimum, contact-change rule, SOR-owned topics.
3. **Redaction:** Luhn-checked card numbers → `card ending 1111`; SSN; full account numbers; phone numbers; email addresses.
   Applied to explicit facts, to events before extraction, and to CRM notes.
4. **Special categories:** health, religion, sexual orientation, political opinion, criminal record are never stored.
   Accessibility needs are allowed only under `contact_and_accessibility_needs` and marked `RESTRICTED`.

Plus: **legal hold** blocks erasure; **DSAR export** returns every record and audit entry; the **audit log**
is append-only and hash-chained (`GET /ops/audit` verifies it, tampering is detected); the **event bus carries
no raw text**, only taxonomy codes and memory references; every answer has a trace of which memories were used.

Production: Cloud DLP for redaction, a policy engine for decisions, audit to WORM storage and the SIEM,
model-risk documentation for the extraction prompts.

## 8. Resilience

| Failure | Behaviour |
|---|---|
| Memory Bank slow | Read budget 800 ms (`read_timeout_s`); on timeout the agent answers from Tier 1 + live profile; the trace says `degraded`. |
| Memory Bank down | Circuit breaker opens after 3 failures; calls are skipped until the half-open probe succeeds. |
| Write fails or is throttled | Outbox with idempotency key (replays don't duplicate), exponential backoff, dead-letter after 5 attempts. A background drainer replays every 3 s. |
| Duplicate event delivery | Event consumers are idempotent on `event_id`. |

Fault injection (`POST /ops/faults` with `ok` | `slow` | `down` | `throttled`) shows each mode in the console.
Code: `cx_memory/resilience.py`, `governed_memory._guarded`.
Production: outbox on Pub/Sub or Firestore drained by a Cloud Run job, DLQ alerting, regional failover per partition.

## 9. Client servicing journeys (digital assistant → specialist, with context)

The client's target experience: the customer uses their channel of choice, the bank anticipates the need,
context and authentication move with the customer when they switch channel, and the specialist knows why the
customer is calling, gets predicted suggested actions and relevant content, and never re-authenticates.

| Journey | Customer · test data | What is checked |
|---|---|---|
| **UC-D1** Predicted fee waiver + offer | Arjun · savings $5.00 monthly service fee; engine predicts `FEE_WAIVER` (0.81); pre-qualified card offer | assistant confirms the predicted intent, waives a deposit fee within its authority, offers the next-best action, transfers to the sales queue in chat with context |
| **UC-D2** Unrecognized charge → live chat | Daniel · July statement with `QXT*DIGITAL SVCS $89.99`; no prediction | NLU intent, statement search, opt-out to an agent; agent greets with the reason, auth carried over, suggested action cites the descriptor article |
| **UC-O1** Digital → voice | as UC-D2, call from the phone on file | click-to-dial with handoff token, IVR redeems it, risk check passes, proficiency routing to fraud/disputes |
| **UC-N** Known-intent fulfilment | Maya · $40.00 minimum due in 2 days; late fee $40.00 (Oct 15), FX fee $6.15; fees team has agents | predicted payment paid in the app → fee inquiry → late-fee waiver outside digital authority → specialist availability → context stored → voice → auto-auth → fees queue → specialist waives → context updated → app shows the outcome on return |
| **UC-NEG** Negative paths | as UC-N | FX fee not waivable, call from an unknown number needs step-up, token replay refused, token expired after 15 min |

How it works:

* **Dialog manager first, LLM second** (`cx_memory/digital.py`). Known servicing intents (payment, fee inquiry, fee
  waiver, unrecognized charge, escalation, next-best offer) are deterministic, so amounts and actions are never
  made up; anything else falls through to the web chat LLM agent with governed memory.
* **Recommendation engine stub** (`data/source_systems/insights.json`): predicted intent with confidence, and the
  next-best (personalization) action.
* **Authority rules** (`cx_memory/servicing.py`): the assistant can waive deposit-account fees up to $35 once per
  12 months; card late fees need a specialist; disclosed FX fees aren't waived.
* **Context store + handoff token** (`cx_memory/context_store.py`): on escalation the assistant stores auth state,
  reason code, a redacted summary, what was already done and referenced transactions, and gets a single-use token
  (15-minute TTL). The token rides on click-to-dial or the chat transfer.
* **Risk-based assessment on channel switch**: channel risk + intent risk + identifier on file + token age.
  Low → authentication carries over (`HIGH`); medium → step-up; high → fraud queue.
* **Proficiency routing** (`data/source_systems/specialists.json`): reason code → skilled queue with someone
  available, else general servicing with a call-back offer (honouring the customer's contact window).
* **Specialist desktop**: why the customer is calling, what was done in the app (don't repeat it), predicted
  suggested actions with eligibility, knowledge articles for the reason code, and the opening line.
* **Fulfilment back to the app**: wrap-up with `fulfilment` updates the context store and memory; the next app
  session opens with the outcome.

Test data and expected results are in `eval/client_use_cases.json`. Run them with
`python eval/run_client_use_cases.py`, with `pytest -q tests/test_client_journeys.py`, or from the console's
**Client test data** tab (scenarios 11–14 play them step by step).

---

## API summary

| Area | Endpoints |
|---|---|
| Channels | `POST /channels/web-chat`, `/channels/voice`, `/channels/email`, `/demo/concurrent`, `/sessions/{id}/end` |
| Identity | `POST /identity/step-up/start`, `/identity/step-up/verify` |
| Digital assistant | `POST /digital/session`, `/digital/message`, `/digital/escalate`; `GET /context/{handoff_token}`, `/customers/{id}/insights` |
| Contact center / CRM | `POST /connect/contact` (with `Attributes.handoff_token` for channel switches), `/connect/contact/{id}/wrap-up`, `GET /crm/activities`, `POST /sor/events` |
| Customer privacy | `GET /customers/{id}/memories`, `/ledger`, `/export`; `POST /customers/{id}/consent`, `/legal-hold`, `/erase` |
| Ops | `GET /ops/status`, `/ops/capacity`, `/ops/audit`, `/ops/events`; `/ops/client-use-cases`; `POST /ops/client-use-cases/run`, `/ops/servicing/reset`, `/ops/faults`, `/ops/outbox/drain`, `/ops/extraction-policy`, `/ops/clock/advance`, `/ops/lifecycle/sweep`, `/ops/burst` |

## What is still simplified

* Offline mode uses keyword search and a scripted fake model; it proves the plumbing and the policies, not answer quality.
  Run `BACKEND=vertex` with Gemini for real answers.
* Ledger, outbox, audit log, live context and the event bus are in-process. Each has a named production store above.
* Redaction and special-category detection are regex-based; production uses Cloud DLP.
* Prices are placeholders.
