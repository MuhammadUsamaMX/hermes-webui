# WebUI Run State Consistency Contract

- **Status:** Proposed
- **Author:** @franksong2702
- **Created:** 2026-05-16
- **Updated:** 2026-09-26
- **Tracking issue:** [#2361](https://github.com/nesquena/hermes-webui/issues/2361)
- **Related architecture:** [#1925](https://github.com/nesquena/hermes-webui/issues/1925), [`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md), [`stable-assistant-turn-anchors.md`](stable-assistant-turn-anchors.md)

## Problem

A single WebUI agent turn is represented by several overlapping state layers:

- the visible transcript the user can read,
- the model context / `context_messages` the agent actually receives,
- `pending_user_message` and active stream metadata,
- live SSE events and in-memory stream state,
- durable run journal / replay state,
- automatic compression summaries and active-task handoff text,
- the browser's live timeline DOM/cache,
- sidebar ordering, unread state, and `updated_at` metadata,
- derived model/context metadata the UI reads (model catalog caches,
  context-window limits) without re-syncing its source.

Those layers are not independent. When they drift apart, the user sees failures
that look unrelated: a prompt is visible but missing from recovered model
context, a live run loses or reorders thinking/tool cards after switching
sessions, cleanup makes old sessions look newly active, replay duplicates content,
or automatic compression reference material appears inside the active turn.

This RFC defines a consistency contract for those layers. It complements the
larger run adapter direction in #1925 by documenting what must remain coherent
while WebUI still has multiple overlapping state stores.

## Goals

- Define the state layers involved in active and recovered WebUI turns.
- Make the source-of-truth expectations explicit for each layer.
- Give reviewers a checklist for streaming, replay, compression, recovery,
  model-context, and sidebar changes.
- Map recent real issues to reusable invariants so future fixes do not solve the
  same class of bug one symptom at a time.

## Non-goals

- Do not implement a runner process, sidecar, or new runtime boundary here.
- Do not replace #1925 or the run adapter contract.
- Do not rewrite the streaming protocol in this RFC.
- Do not reopen already-fixed narrow bugs.
- Do not make this a catch-all for unrelated UI polish.

## Current implementation relationship

Stable Assistant Turn Anchors now implement the presentation/reconciliation
portion of this contract for one assistant turn. The run journal and settled
transcript provide durable observations; the Anchor registry and
`activity_scene_v1` reconcile those observations into Compact Worklog,
Transparent Stream, or Final answer only; `S.messages`, `INFLIGHT`, renderer
caches, and DOM remain projections or recovery caches rather than independent
semantic owners.

This RFC remains `Proposed` because its broader cross-layer contract also covers
model-context reconstruction, compression handoff, session metadata, and future
runtime-adapter migration. Shipped Anchor coverage strengthens invariants 2, 3,
and 5; it does not mark every run-state boundary implemented.

## State Layers

| Layer | Purpose | Source-of-truth expectation | Must not do |
|---|---|---|---|
| Visible transcript | Shows what the user and assistant said | Session transcript plus live replay should produce one chronological user-visible story | Hide the user turn that started active work, or show internal recovery text as current user intent |
| Model context / `context_messages` | Supplies conversation state to the agent | Must include the current visible user turn unless deliberately excluded with a user-visible reason | Let the agent resume from context that contradicts what the user can see |
| Pending turn metadata | Bridges submitted-but-not-yet-finalized user input | Must identify the user turn and stream that own active work | Become a permanent duplicate transcript row after recovery |
| Live stream / SSE | Delivers active runtime events to the browser | Must remain an observation path, not the only durable truth for already-emitted events | Lose the visible scene on refresh, reconnect, or session switch |
| Worker lifecycle registry (`ACTIVE_RUNS`) | Tracks whether a worker still occupies the session, so a successor turn cannot start on top of it | Broader than "attachable UI work": a cancelled worker stays registered while it unwinds | Be read directly as the set of runs a browser may attach to |
| Run journal / replay | Rebuilds emitted runtime events after reconnect or restart | Must be cursor-safe and idempotent | Duplicate assistant text, thinking text, tool cards, or compression cards |
| Compression summary / handoff | Gives the agent recovery context after automatic compression | Must remain agent-facing recovery material unless explicitly rendered as history | Pollute the active turn or become implicit current user intent |
| Live UI scene/cache | Preserves expanded rows, in-progress cards, local scroll, and transient grouping | May optimize presentation but must be rebuildable or degradable from transcript/replay | Become the only place where chronological ordering exists |
| Sidebar/session metadata | Helps the user find active and recent sessions | Must reflect meaningful user or assistant activity | Treat background cleanup as a fresh user-facing update |
| Derived model/context metadata | Projects model catalog and context-window limits into pickers, context bars, and compression thresholds | Must re-sync from its source config or `/api/models` before it is rendered or acted on | Outlive its source (stale TTL cache, stale limit after resume or model switch) and drive thresholds silently |

### Authority matrix

The table above says what each layer is for. This matrix is the reviewable
form of that contract: for every layer, who owns it, how long it persists, what
divergence is tolerated, and how replay/recovery must treat it. Anchors are
symbol names, never line numbers, so the matrix stays true across source-layout
shifts (#5513, #5542).

| Layer | Authority | Persistence lifetime | Allowed divergence | Replay / recovery rule |
|---|---|---|---|---|
| Visible transcript | Settled sidecar session file (`SESSION_DIR`, `Session.messages`); while a turn runs, the streamed scene feeding it | Durable on disk until the session is deleted; `.json.bak` retained for recovery | May trail the live stream by in-flight events; may downgrade to labeled structured replay, never to silently reordered rows | Rebuild chronologically from sidecar rows plus run journal events; never from the browser cache |
| Model context (`context_messages`) | Server-side reconstruction over `Session.context_messages` at handoff time | Rebuilt per turn; persisted only as far as the sidecar persists it | May differ from the visible transcript only for deliberately excluded turns, with the reason shown to the user | Recovery must re-include the visible or pending user turn (invariant 1) before any continuation is requested |
| Pending turn metadata | `pending_user_message` with `pending_started_at` / `pending_user_source` on the session record | From submit until the turn is checkpointed into `Session.messages` and the field is cleared | Metadata only; must never become a second transcript row | Turn journal (`TURN_JOURNAL_DIR_NAME`) re-derives state; `_latest_user_matches_pending_text` decides whether a recovered pending turn is already checkpointed |
| Live stream / SSE | Observation path only: `STREAMS` channels and the session events routes | Process memory, per stream; gone on restart | May lose events on disconnect; anything already emitted must remain recoverable elsewhere | Replay from `RUN_JOURNAL_DIR_NAME` with a cursor; live and replayed events share one renderer |
| Worker lifecycle registry (`ACTIVE_RUNS`) | Occupancy — whether a worker still owns the session | Process memory; cancelling rows reclaimed after the bounded unwind window once they own no live `STREAMS` channel | Broader than attachable UI work (invariant 9) | Never replayed; re-derived empty at startup and repopulated by live work |
| Run journal / replay | Emitted runtime events: ordering, seq cursors, terminal states | `_run_journal` JSONL under `SESSION_DIR`, append-only, bounded snapshot args | Snapshot argument values may be truncated; event identity and `seq` must not change | Cursor-safe and idempotent: a resumed cursor never re-delivers settled events or duplicates cards |
| Compression summary / handoff | `compression_anchor_*` session fields produced by `is_context_compression_marker()` | Retained as anchor/recovery metadata; live-only divider rows are omitted from settled history | Agent-facing recovery material may exist with no matching user-visible row | Render as a quiet non-interactive divider only; later tool, reasoning, or interim events prove the barrier passed |
| Live UI scene/cache | None — presentation only: `INFLIGHT`, `INFLIGHT_STATE_*`, renderer caches, DOM | Tab-local; localStorage snapshots are best-effort and cleared on teardown | May be stale, degraded, or partially rebuilt | Rebuildable from transcript plus replay; if it cannot be, downgrade to explicit structured replay (invariant 3) |
| Sidebar/session metadata | Projection: `SESSION_INDEX_FILE` (`_index.json`) and the session list cache; counts come from the session store | Durable but derived; pruned and rebuilt by recovery (`_rebuild_recovery_session_index`) | May lag counts briefly; must never be refreshed by maintenance as if it were activity (invariant 4) | Rebuilt after recovery or repair so restored rows appear immediately |
| Derived model/context metadata | Source config (`config.yaml`, `_PROVIDER_MODELS`) and the `/api/models` response | Cache only: `STATE_DIR/models_cache.json` via `_get_models_cache_path`, plus in-memory `_available_models_cache` / `_available_models_cache_ts` TTL | May lag its source only within the TTL, and must be invalidated when the source changes (#2443) | After a session resume or model switch, the UI re-syncs from the server before rendering context windows or compression thresholds (#2442) |

## Core Invariants

1. **Visible current turns enter model context.** If the user can see a current
   prompt and WebUI asks the model to continue that work, the prompt must be in
   the reconstructed model context unless WebUI shows an explicit reason it was
   excluded.
2. **Active turn UI keeps its owner.** The user turn that started active work
   must remain visible before assistant text, thinking cards, tool cards, or
   activity groups that belong to that work.
3. **Reattach preserves order or degrades clearly.** Refresh, reconnect, and
   session switch must preserve chronological live-scene order. If WebUI cannot
   restore the exact live scene, it should downgrade to an explicit structured
   replay state instead of silently reordering content.
4. **Maintenance is not activity.** Runtime maintenance such as stale-stream
   cleanup, orphan repair, or background compression must not refresh sidebar
   ordering, unread markers, or active-session affordances as if the user or
   assistant just acted.
5. **Replay is idempotent.** Replaying a run from a cursor must not duplicate
   transcript rows, thinking content, interim assistant text, tool cards, or
   compression cards. Replayed long-task events should enter the same
   browser-facing timeline renderer as live SSE events so recovery does not
   downgrade a structured Thinking / progress / tool / compression turn into a
   separate flattened presentation.
   When session loading combines a WebUI sidecar with Hermes Agent `state.db`, a
   native-image user turn may appear as both rich multipart content and scalar
   text that replaces each image part with `[screenshot]`. Reconciliation may
   treat those rows as one turn only when the multipart value contains text and
   recognized native-image parts, its exact scalar projection matches, role and
   tool shape match, timestamps match exactly, stable IDs and provider metadata
   do not conflict, and the pairing is unambiguous. Keep the rich sidecar row;
   if any requirement is missing or contradictory, preserve both rows rather
   than deduplicating. Literal scalar `[screenshot]` text alone is not identity
   evidence.
   Visible interim assistant progress must remain visible timeline content; a
   compact Activity disclosure may summarize adjacent tool/debug detail, but it
   must not be the only place where the user can see emitted progress text.
6. **Compression is not current intent.** Automatic compression summaries and
   reference cards are recovery/handoff material. They must not be treated as a
   new user request, active-turn content, or the default visible explanation for
   the current answer.
   Automatic compression may appear during a live turn only as a quiet,
   non-interactive context divider in the Worklog timeline, not as a clickable
   tool row. It should use action wording: `Compressing context` while active
   and `Context auto-compressed` when the agent has continued past the
   compression barrier or when a completion event arrives. The timer is
   diagnostic detail, not the source of truth for the divider's running state.
   Later tool, reasoning, or interim assistant events prove the compression
   barrier has passed even if no explicit completion event was delivered.
   Settled final history should omit live-only automatic-compression rows unless
   there is a user-visible recovery or error state to explain.
7. **Observation has a degraded path.** Long-running or many-session observation
   should expose enough heartbeat/degraded status that the UI does not appear
   silent and ordinary APIs do not stall behind active streams.
8. **Every mutation names its layer.** A PR touching streaming, recovery,
   context reconstruction, compression, replay, or sidebar metadata should state
   which layer it changes and what regression proves the invariant still holds.
9. **Lifecycle-busy is not client-attachable.** `ACTIVE_RUNS` answers "may a new
   turn start?", not "may a browser attach a renderer?". Cancellation splits the
   two: `cancel_stream()` keeps the row as `phase="cancelling"` so a successor
   cannot overlap the unwinding worker, but the client has already reached a
   terminal state for that stream because its run journal ends in a terminal
   event. Recovery paths that hand a stream id to a renderer — session SSE
   recovery and hidden-tab status polling — must therefore exclude cancelling
   rows, while busy/admission checks must keep counting them. Reading the
   registry with a single meaning resurrects a cancelled run on every fresh
   subscription: the client attaches, consumes the terminal event, tears the
   renderer down, resubscribes, and the loop repeats indefinitely.

   Because a cancelling row can otherwise persist forever, cancellation unwind is
   bounded: a cancelling row older than that window **and** owning no live
   `STREAMS` channel is reclaimed from `ACTIVE_RUNS` along with its stream-owner
   entry, so a wedged worker cannot suppress background wakeups permanently.
   Reclamation requires both conditions — age alone must not evict a row that
   still owns a live channel. Staleness is measured from the cancellation
   timestamp (falling back to run start), so a long-running turn cancelled
   moments ago is never mistaken for an orphan.

10. **Derived state is subordinate to its source.** Persisted caches,
    in-memory TTL caches, optimistic client flags, display counts, and sidebar
    rows project state they do not own. When a projection and its source
    disagree, the source wins, and a change at the source must invalidate or
    re-sync every downstream projection before it is rendered or acted on: a
    model catalog cache must not outlive a provider config change (#2443),
    context-window metadata must be re-synced after a session resume or model
    switch before the UI computes compression thresholds (#2442), and a stale
    client-side busy or optimistic flag must never block a new turn or override
    canonical idle server rows (#2796).
11. **Recovery leaves provenance, not only content.** Startup or repair that
    restores state from a backup or `state.db` (`recover_session()`,
    `recover_missing_sidecars_from_state_db()`) must also persist content-free
    metadata — recovered_from, recovered_at, recovery_reason, before/after
    message counts — that downstream consumers and audit or health endpoints
    can use to stay idempotent, and must rebuild derived indexes
    (`SESSION_INDEX_FILE`) so projections reflect restored state immediately.
    That provenance is maintenance, never user activity (invariant 4), and an
    intentional delete must not be resurrected by orphan-backup recovery.

## Review Checklist

Use this checklist for PRs that touch run state, streaming, replay, compression,
context reconstruction, or session metadata:

- Which state layers does this PR read or write?
- Which layer is the source of truth after this change?
- Can the visible transcript and model context diverge? If yes, is that
  deliberate and user-visible?
- What happens after browser refresh, session switch, SSE reconnect, and WebUI
  restart?
- Does replay rebuild the same scene without duplicates?
- Does replay use the same timeline-rendering path as live SSE for thinking,
  interim assistant text, tool cards, compression cards, and terminal states?
- Can this change move a session in the sidebar without meaningful user or
  assistant activity?
- Does this change read `ACTIVE_RUNS` for admission ("may a turn start?") or for
  attachment ("may a browser render this?"), and does it use the matching
  predicate for that question?
- If it introduces or changes a reclamation window, what proves an in-flight
  cancellation is not evicted early, and that a wedged one is eventually freed?
- Can automatic compression or recovery text become visible active-turn content?
- Which derived caches or client projections does this read or write, and what
  invalidates them when their source changes (invariant 10)?
- After a session resume or model switch, which metadata must be re-synced
  before the UI renders context windows, thresholds, or counts?
- Does any client-side optimistic or busy flag override canonical server state?
- If this restores state from a backup or `state.db`, what recovery provenance
  is persisted, which derived indexes are rebuilt, and how is an intentional
  delete told apart from an orphaned backup (invariant 11)?
- What test or manual evidence proves the invariant?

## Existing Issue Map

| Example | State boundary exposed | Layer | Relevant invariant |
|---|---|---|---|
| [#2341](https://github.com/nesquena/hermes-webui/issues/2341) / [#2342](https://github.com/nesquena/hermes-webui/pull/2342) | Active reattach could show agent activity without the pending user turn that started it | Pending turn metadata, Live UI scene/cache | 2 |
| [#2344](https://github.com/nesquena/hermes-webui/issues/2344) / [#2347](https://github.com/nesquena/hermes-webui/pull/2347) | Session switching could lose or reorder the live thinking/tool/interim timeline | Live stream / SSE, Live UI scene/cache | 3, 5 |
| [#2345](https://github.com/nesquena/hermes-webui/issues/2345) / [#2349](https://github.com/nesquena/hermes-webui/pull/2349) | Stale stream cleanup could mutate `updated_at` and resurface old sessions | Sidebar/session metadata | 4 |
| [#2346](https://github.com/nesquena/hermes-webui/issues/2346) / [#2348](https://github.com/nesquena/hermes-webui/pull/2348) | Thinking cards could repeat interim assistant progress text | Live UI scene/cache | 5 |
| [#2353](https://github.com/nesquena/hermes-webui/issues/2353) / [#2354](https://github.com/nesquena/hermes-webui/pull/2354) | Recovered pending user turns could be visible but missing from model context | Model context, Pending turn metadata | 1 |
| [#2355](https://github.com/nesquena/hermes-webui/issues/2355) / [#2357](https://github.com/nesquena/hermes-webui/pull/2357) | Auto-compression rotation could leave reference-only cards in the active conversation tail | Compression summary / handoff, Visible transcript | 3, 6 |
| [#2308](https://github.com/nesquena/hermes-webui/issues/2308) / [#2309](https://github.com/nesquena/hermes-webui/pull/2309) | Compressed sessions could resume stale agent tasks when the user starts an ordinary fresh chat | Compression summary / handoff | 6 |
| [#2283](https://github.com/nesquena/hermes-webui/pull/2283) | Run event journal replay provides the foundation for ordered recovery | Run journal / replay | 5 |
| [#2442](https://github.com/nesquena/hermes-webui/issues/2442) / [#2444](https://github.com/nesquena/hermes-webui/pull/2444) | Context-window metadata could stay stale after a session resume or model switch, driving premature compression | Derived model/context metadata | 10 |
| [#2443](https://github.com/nesquena/hermes-webui/issues/2443) | A persisted model-list cache could outlive the provider config change that should have replaced it | Derived model/context metadata | 10 |
| [#2796](https://github.com/nesquena/hermes-webui/pull/2796) / [#2797](https://github.com/nesquena/hermes-webui/pull/2797) / [#2801](https://github.com/nesquena/hermes-webui/pull/2801) | Stale optimistic busy state, non-deduped display counts, and session-level `tool_calls` overriding settled message metadata | Live UI scene/cache, Sidebar/session metadata | 10 |
| [#4208](https://github.com/nesquena/hermes-webui/pull/4208) / [#4221](https://github.com/nesquena/hermes-webui/pull/4221) | Coarse polling or focus recovery could refresh the active transcript late or without a session id | Live stream / SSE, Sidebar/session metadata | 3, 4 |
| [#4216](https://github.com/nesquena/hermes-webui/pull/4216) | Reconciliation could drop `state.db`-only user prompts that predate a newer sidecar tail | Visible transcript, Model context | 1 |
| [#4213](https://github.com/nesquena/hermes-webui/pull/4213) / [#4218](https://github.com/nesquena/hermes-webui/pull/4218) | Sidebar and lineage projections could hide real TUI-origin or multi-row session history | Sidebar/session metadata, Visible transcript | 10 |

These references are evidence for the contract. This RFC does not make the
linked implementation PRs dependent on this document, and it does not close the
tracking issue by itself.

## Relationship To The Run Adapter RFC

The run adapter RFC defines the longer-term event/control boundary for WebUI and
Hermes runtime ownership. This RFC defines the consistency rules that the current
WebUI and any future adapter-backed implementation must preserve.

The two documents should be read together:

- The adapter contract answers: "Where should execution ownership live?"
- This consistency contract answers: "How do transcript, context, streams,
  replay, compression, and UI metadata stay coherent while execution is active
  or being recovered?"

## Rollout Plan

1. Land this RFC as a reviewable draft and refine it through PR discussion.
2. Link future streaming/recovery/compression/sidebar PRs back to the invariant
   they intentionally preserve or change.
3. Convert recurring checklist items into focused regression tests where
   practical.
4. If #1925 introduces a new adapter-backed runtime layer, update this RFC or
   replace it with the accepted implementation contract so these invariants do
   not live only in historical discussion.
