"""Export the official installed application code, excluding runtime data and secrets."""

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
ITEMS = (
    "application", "bin", "client", "install", "public", "html", "schema",
    "bootstrap.php", "command.php", "cron.php", "daemon.php", "index.php",
    "clear_cache.php", "rebuild.php", "upgrade.php", "websocket.php",
    "extension.php", "preload.php", "composer.json", "composer.lock", "LICENSE.txt", ".htaccess",
)


def main() -> None:
    destination = ROOT / "espocrm"
    if (destination / "application").exists():
        raise SystemExit("Source already exported; refusing to overwrite it.")
    container = subprocess.check_output(
        ["docker", "compose", "ps", "-q", "espocrm"], cwd=ROOT, text=True,
    ).strip()
    if not container:
        raise SystemExit("Start EspoCRM before exporting source.")
    destination.mkdir(exist_ok=True)
    php = "echo json_encode(array_values(array_diff(scandir('/var/www/html'), ['.', '..'])));"
    available = json.loads(subprocess.check_output(
        ["docker", "exec", container, "php", "-r", php], text=True,
    ))
    for item in ITEMS:
        if item in available:
            subprocess.run(["docker", "cp", f"{container}:/var/www/html/{item}", str(destination / item)], check=True)
    provenance = {
        "repository": "https://github.com/espocrm/espocrm.git",
        "release": "10.0.9",
        "image": "espocrm/espocrm:10.0.9",
        "source": "Official built release exported from its Docker image; not a Git checkout",
        "excluded": ["data", "vendor", "runtime credentials"],
    }
    (destination / "SOURCE.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")
    print("Official EspoCRM application code exported to espocrm/; runtime data excluded.")


if __name__ == "__main__":
    main()
