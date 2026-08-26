# Job event stream contract

`GET /api/v1/events?token=<short-lived-token>&after=<sequence>` returns `text/event-stream`.

- `id` is durable monotonically increasing event sequence.
- `event` is `job` for state/progress, `heartbeat` for an idle keepalive, or `reset` when the replay cursor predates retained history.
- `job` data is one compact JSON JobEvent. `heartbeat` data is `{}`. `reset` data is a separate compact JSON object with integer `minimumSequence` and `latestSequence`; it is not a JobEvent.
- Client reconnects with `after` set to last processed sequence. `Last-Event-ID` is also accepted.
- Replay is ordered, exclusive of the cursor (only events whose sequence is greater), and bounded to 1,000 events per connection.
- A terminal event is emitted once for its durable transition; clients then stop waiting for that job. The global stream recycles after 20 seconds and EventSource reconnects with a newly issued token. Polling `GET /api/v1/jobs/{jobId}` remains equivalent fallback.
- Event history retains the most recent 100,000 events. A cursor older than retained history receives one `reset` event with `minimumSequence` and `latestSequence`; the client refreshes canonical job resources and resumes from `latestSequence`.
- Event token is session-bound, single-origin, short-lived, and carries no filesystem authority.

Job states: `QUEUED`, `RUNNING`, `CANCEL_REQUESTED`, `CANCELED`, `SUCCEEDED`, `FAILED`, `INTERRUPTED`, `FAILED_RECOVERABLE`.
