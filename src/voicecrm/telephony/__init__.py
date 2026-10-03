"""Telephony module: Asterisk, SIP trunk with the city number, extensions 101–130, queues and transfers.

Status: not started. Planned responsibilities (docs/ROADMAP.md, stages 3–5):
- directory of employees and extensions, queues and working hours;
- call state machine with its own call_id, independent of dialogue session ids;
- bridging the voice robot into a call through ARI External Media or the Asterisk media WebSocket;
- transfers only to directory entries validated by the server, never to model-chosen SIP addresses;
- fallback to ordinary PBX routing when the robot is unavailable.
"""
