"""Report benchmark prerequisites without printing secret values."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    # Load .env if present (does not override existing env).
    env_path = ROOT / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
            print("dotenv_loaded=True")
        except Exception as exc:
            print(f"dotenv_loaded=False error={type(exc).__name__}")
    else:
        print("dotenv_loaded=False missing=.env")

    keys = [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "VERTEX_PROJECT",
        "VERTEX_LOCATION",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "PHASE2_RECOVERY_ENABLED",
        "OCR_ENGINE",
    ]
    for key in keys:
        value = os.getenv(key)
        if value:
            print(f"{key}=set len={len(value)}")
        else:
            print(f"{key}=unset")

    cred = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or ""
    if cred:
        print(f"credentials_file_exists={Path(cred).is_file()}")

    for rel in (".env", "credentials.json", "service-account.json"):
        path = ROOT / rel
        print(f"path {rel} exists={path.exists()}")

    try:
        from services.vertex_gemini_client import initialize_vertex_gemini_client

        client = initialize_vertex_gemini_client()
        print(f"vertex_client_ok={client is not None}")
    except Exception as exc:
        print(f"vertex_client_ok=False error={type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
