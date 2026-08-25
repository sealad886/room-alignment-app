# ADR 0002: SQLite plus JSON project documents

Status: Accepted.

Use SQLite for durable library/media/job indexing and JSON project documents for evolving editorial schema. This keeps transactional local storage while allowing forward-compatible custom provenance. Migrations must be added before schema changes after v1.

