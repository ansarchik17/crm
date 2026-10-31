"""EspoCRM is the CRM system of record; voice tools use its server-only adapter.

Current adapter: read contacts by phone, verify reachability, create Calls/Tasks
through typed server methods. Callback tools prepare immutable local drafts;
explicit local confirmation dispatches at most one POST per durable operation.
Unknown writes are reconciled by GET. Automatic delivery/outbox and confirmed
appointment booking are still future work.
"""
