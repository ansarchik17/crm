"""CRM module: delivery of call outcomes to the customer's CRM.

Status: not started; the target CRM is not chosen yet (docs/ROADMAP.md, stage 6).
Planned responsibilities:
- minimal payload per call: call_id, contact, topic, responsible employee, outcome, recording link, next steps;
- outbox with retries and an idempotency key per (company, event) so redelivery never duplicates leads;
- writes happen only after the system confirms an action, never on the model's claim alone.
"""
