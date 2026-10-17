from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .models import DATAFLOW_BASE_URL, DEFAULT_API_BASE_URL


class CredentialsError(Exception):
    """Raised when the credentials file is missing or malformed."""


def load_credentials(
    path: Path = Path("config/credentials.json"),
) -> dict:
    """
    Load Productsup credentials.

    Expected file layout:

        {
          "productsup": {
            "token": "client_id:client_secret",
            "base_url": "https://platform.productsup.com",
            "dataflow_base_url": "https://platform.productsup.com"
          }
        }

    Returns:
        {
          "token": str,
          "base_url": str,             # REST API host
          "dataflow_base_url": str,    # dataflow page host
        }

    `base_url` and `dataflow_base_url` fall back to their respective
    defaults when missing, empty, or None. Only `token` is required.
    """
    if not path.exists():
        raise CredentialsError(
            f"Credentials file not found: {path}. "
            'Expected format: {"productsup": {"token": "...", '
            '"base_url": "...", "dataflow_base_url": "..."}}'
        )

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise CredentialsError(f"Invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise CredentialsError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise CredentialsError(f"{path} must contain a JSON object at the top level.")

    block = data.get("productsup")
    if block is None:
        raise CredentialsError(f"{path}: missing 'productsup' object.")
    if not isinstance(block, dict):
        raise CredentialsError(f"{path}: 'productsup' must be a JSON object.")

    # -- token ------------------------------------------------------------
    raw_token = block.get("token")
    if raw_token is None:
        raise CredentialsError(f"{path}: 'productsup.token' is missing.")
    if not isinstance(raw_token, str):
        raise CredentialsError(
            f"{path}: 'productsup.token' must be a string, "
            f"got {type(raw_token).__name__}."
        )
    token = raw_token.strip()
    if not token:
        raise CredentialsError(f"{path}: 'productsup.token' is empty.")

    # -- base_url (REST API) ----------------------------------------------
    base_url = _read_url_field(block, "base_url", DEFAULT_API_BASE_URL, path)

    # -- dataflow_base_url (dataflow page) --------------------------------
    dataflow_base_url = _read_url_field(
        block, "dataflow_base_url", DATAFLOW_BASE_URL, path
    )

    return {
        "token": token,
        "base_url": base_url,
        "dataflow_base_url": dataflow_base_url,
    }


def _read_url_field(
    block: Mapping[str, Any],
    key: str,
    default: str,
    path: Path,
) -> str:
    """
    Read an optional URL field from the 'productsup' block.

    Rules:
        * missing / None     -> default
        * non-string         -> CredentialsError
        * empty after strip  -> default
        * otherwise          -> stripped, trailing '/' removed
    """
    raw = block.get(key)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise CredentialsError(
            f"{path}: 'productsup.{key}' must be a string, "
            f"got {type(raw).__name__}."
        )
    stripped = raw.strip()
    if not stripped:
        return default
    return stripped.rstrip("/")


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]{8,}:[A-Za-z0-9_\-]{8,}")


def redact_token(text: str) -> str:
    """
    Mask any 'client_id:client_secret'-like substring that appears in
    logs or error messages. Safe to call on arbitrary text.
    """
    if not text:
        return text

    def _mask(match: "re.Match[str]") -> str:
        value = match.group(0)
        if len(value) <= 8:
            return value
        return f"{value[:4]}...{value[-4:]}"

    return _TOKEN_PATTERN.sub(_mask, text)
