"""Analytics module: call recordings, post-call transcription and AI analysis.

Status: not started (docs/ROADMAP.md, stage 7).
Planned responsibilities:
- recordings of every leg, including the employee leg after transfer, stored privately with expiring links;
- background queue: file STT (the existing Alem adapter fits here) followed by OpenAI analysis;
- schema-validated result: reason, summary, outcome, agreements, next action, timestamped quote;
  empty fields when there is no evidence, sentiment shown as an estimate.
"""
