"""Vertex AI authentication and client helpers (§13).

Centralizes all project / location / credentials resolution so that
``core/llm.py`` and ``core/voice_live.py`` have a single source of truth.

Auth priority:
1. Service-account JSON at the path in ``GOOGLE_APPLICATION_CREDENTIALS``
   (or the repo-root ``roba.json`` default).  Loaded via
   ``google.oauth2.service_account.Credentials`` with the
   ``cloud-platform`` scope.
2. Application Default Credentials (ADC) — falls through when no service-
   account file is found.  Covers ``gcloud auth application-default login``
   on workstations and Workload Identity on GCP-hosted environments.

Project resolution priority:
1. ``GOOGLE_CLOUD_PROJECT`` env var.
2. ``project_id`` field inside the service-account JSON (if the file exists).

Google Gen AI SDK requirement:
  ``pip install google-genai>=1.0.0 google-auth>=2.0.0``
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Scope required for all Vertex AI / Gemini endpoints.
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Repo-root default for the service-account JSON key.
_DEFAULT_SA_FILENAME = "roba.json"


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def service_account_path() -> Optional[Path]:
    """Return the path to the service-account JSON file, or ``None``."""
    raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", _DEFAULT_SA_FILENAME)
    path = Path(raw)
    if not path.is_absolute():
        # Resolve relative to the repository root (the directory that contains
        # this file's package — two levels up from core/).
        repo_root = Path(__file__).resolve().parent.parent
        path = repo_root / path
    return path if path.exists() else None


def vertex_project() -> str:
    """Return the GCP project ID, or empty string if unresolvable."""
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if project:
        return project
    # Fall back to project_id in the service-account JSON.
    sa_path = service_account_path()
    if sa_path:
        try:
            data = json.loads(sa_path.read_text())
            return str(data.get("project_id", "")).strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read project_id from %s: %s", sa_path, exc)
    return ""


def vertex_location() -> str:
    """Return the GCP region, defaulting to ``us-central1``."""
    return os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1").strip() or "us-central1"


def vertex_credentials() -> Optional[Any]:
    """Return service-account credentials, or ``None`` to use ADC.

    Returns ``None`` (not ``False``) so callers can pass the value directly
    to ``genai.Client(credentials=...)`` — ``None`` tells the SDK to use ADC.
    """
    sa_path = service_account_path()
    if sa_path is None:
        return None
    try:
        from google.oauth2.service_account import Credentials  # type: ignore[import-untyped]

        return Credentials.from_service_account_file(
            str(sa_path), scopes=[_CLOUD_PLATFORM_SCOPE]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load service-account credentials from %s: %s", sa_path, exc)
        return None


def vertex_available() -> bool:
    """Return ``True`` when a GCP project is resolvable.

    Used as the provider-availability gate instead of checking for an API key.
    ``False`` causes the LLM provider to skip Vertex and fall through to the
    canned response — identical to the original no-key behavior.
    """
    return bool(vertex_project())


def build_genai_client() -> Any:
    """Build and return a ``google.genai.Client`` configured for Vertex AI.

    The client is lightweight to construct — callers should cache it
    (e.g. ``LLMProvider._gemini_client``).

    Raises ``ImportError`` if ``google-genai`` is not installed, and
    ``RuntimeError`` if no GCP project can be resolved.
    """
    from google import genai  # type: ignore[import-untyped]

    project = vertex_project()
    if not project:
        raise RuntimeError(
            "Cannot connect to Vertex AI: no GOOGLE_CLOUD_PROJECT env var set "
            "and no project_id found in roba.json."
        )
    location = vertex_location()
    credentials = vertex_credentials()
    return genai.Client(
        vertexai=True,
        project=project,
        location=location,
        credentials=credentials,  # None → SDK uses ADC
    )
