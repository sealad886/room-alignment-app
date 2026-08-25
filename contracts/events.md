# Job event stream contract

`GET /api/v1/events?token=<short-lived-token>&after=<sequence>` returns `text/event-stream`.

- `id` is durable monotonically increasing event sequence.
- `event` is `job` for state/progress and `heartbeat` for an idle keepalive.
- `data` is one compact JSON JobEvent.
- Client reconnects with `after` set to last processed sequence. `Last-Event-ID` is also accepted.
- Replay is ordered, inclusive only of events whose sequence is greater than cursor, and bounded to 1,000 events per connection.
- Stream closes after terminal replay or 20 seconds; EventSource reconnects. Polling `GET /api/v1/jobs/{jobId}` remains equivalent fallback.
- Event token is session-bound, single-origin, short-lived, and carries no filesystem authority.

Job states: `QUEUED`, `RUNNING`, `CANCEL_REQUESTED`, `CANCELED`, `SUCCEEDED`, `FAILED`, `INTERRUPTED`, `FAILED_RECOVERABLE`.
