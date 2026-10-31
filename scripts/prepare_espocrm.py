"""Create local EspoCRM installation secrets without replacing existing values."""

from pathlib import Path
import secrets


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    directory = ROOT / "runtime" / "secrets"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    for name in ("espo_db_password", "espo_db_root_password", "espo_admin_password"):
        path = directory / name
        if not path.exists():
            with path.open("x") as secret_file:
                secret_file.write(secrets.token_urlsafe(32) + "\n")
        path.chmod(0o600)
    print("EspoCRM secrets ready; existing values preserved. No secrets printed.")


if __name__ == "__main__":
    main()
