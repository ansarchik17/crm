"""Validate and install the project's standalone Codex agent profiles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_NAMES = (
    "crm_architect",
    "voice_backend",
    "realtime_audio",
    "latency_qa",
    "crm_integrations",
    "telephony_engineer",
    "call_analytics",
    "knowledge_quality",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate without writing")
    parser.add_argument("--update", action="store_true", help="Back up and update existing project profiles")
    args = parser.parse_args()
    source = PROJECT_ROOT / "agents" / "profiles"
    target = PROJECT_ROOT / ".codex" / "agents"
    profiles: list[tuple[str, bytes]] = []

    for name in PROFILE_NAMES:
        raw = (source / f"{name}.toml").read_bytes()
        profile = tomllib.loads(raw.decode("utf-8"))
        required = {"name", "description", "developer_instructions"}
        if set(profile) != required:
            raise ValueError(f"{name}: expected only {sorted(required)}")
        if any(not isinstance(profile[key], str) or not profile[key].strip() for key in required):
            raise ValueError(f"{name}: required fields must be nonempty strings")
        if profile["name"] != name:
            raise ValueError(f"{name}: profile name must match filename")
        profiles.append((name, raw))

    if args.check:
        print(f"Validated {len(profiles)} project agent profiles; no files written.")
        return 0

    # Preflight all destinations before writing any profile. Refuse symlinks and
    # unrelated edits instead of silently replacing another user's configuration.
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError("Agent directory must not be a symbolic link")
    for name, raw in profiles:
        destination = target / f"{name}.toml"
        if destination.is_symlink():
            raise ValueError(f"Refusing symbolic link: {destination}")
        if destination.exists() and destination.read_bytes() != raw and not args.update:
            raise ValueError(f"Refusing to replace a different profile: {destination}")

    target.mkdir(parents=True, exist_ok=True)
    changed = [target / f"{name}.toml" for name, raw in profiles
               if (target / f"{name}.toml").exists() and (target / f"{name}.toml").read_bytes() != raw]
    if changed:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = PROJECT_ROOT / "data" / "backups" / ("project-agents-" + timestamp)
        backup.mkdir(parents=True)
        for destination in changed:
            shutil.copy2(destination, backup / destination.name)
    for name, raw in profiles:
        destination = target / f"{name}.toml"
        if not destination.exists():
            with destination.open("xb") as profile_file:
                profile_file.write(raw)
        elif args.update and destination.read_bytes() != raw:
            destination.write_bytes(raw)
    print(f"Installed {len(profiles)} project agent profiles in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
