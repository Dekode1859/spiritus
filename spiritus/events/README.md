# Spiritus Events

Spiritus exposes OpenCode events through a UI event bus and state
synchronization layer.

In this implementation events originate from **OpenCode's SSE stream**
(`GET /event`) and are routed by the **UI event bus in `spiritus/ui/app.js`**
(`connectSSE` → `handleOCEvent`). The events are generic runtime signals:

- `message.updated` — message lifecycle (role, completion)
- `message.part.updated` / `message.part.delta` — streaming content
- `session.status` / `session.idle` — busy/idle working state
- `session.updated` / `session.deleted` — session list changes
- `session.error` — runtime errors

None of these carry domain meaning. The UI bus turns them into view-state
updates (streaming bubbles, working indicator, session list) without knowing
what the app *is*.

This package is intentionally code-free on the Python side; the event bus lives
in the Spiritus UI where the events are consumed.
