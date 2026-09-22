# UAT Runbook — product-template (Service Template)

## Feature
Standard entity management and health verification

## Preconditions
- Service is configured with valid environment (`.env.local` or `.env.example`).
- Database migrations have been applied via `make migrate`.
- Required port is free and accessible.

## Steps
1. Start service locally using `python app.py --serve --port 8080`
2. Send HTTP GET to `http://localhost:8080/health` and verify status is healthy
3. Send HTTP POST to `http://localhost:8080/api/v1/items` with test JSON payload
4. Verify HTTP 201 Created and item persisted in database

## Expected Results
- Service starts cleanly and binds port 8080
- Health probe returns HTTP 200 with healthy status and database connection active
- Item record is successfully stored and retrievable via GET /api/v1/items

---
*Author: QA & Primary Owner (MasterSpec Section 31.1, Section 33.1)*
*Verification Contract: `verification/contract.yaml`*
