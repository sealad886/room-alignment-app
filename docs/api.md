# Local API

All endpoints use JSON and bind to loopback by default.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/health` | Service health/version |
| GET | `/api/libraries` | Indexed libraries |
| POST | `/api/scans` | Start folder scan (`path`, optional `limit`) |
| GET | `/api/scans/{id}` | Scan progress/result |
| GET | `/api/media?libraryId=…` | Paginated indexed media |
| GET/POST | `/api/projects` | List/save project documents |
| GET | `/api/projects/{id}` | Load project |
| GET | `/api/projects/{id}/preflight` | Validate render plan |
| GET | `/api/projects/{id}/manifest` | Produce edit-decision manifest |
| POST | `/api/projects/{id}/render` | Start FFmpeg render |
| GET | `/api/render/{id}` | Render status |
| POST | `/api/render/{id}/cancel` | Cancel running render |

Service has no authentication because default listener is local-only. Non-loopback binding is unsupported security posture and requires an authenticated reverse proxy plus explicit threat review.

