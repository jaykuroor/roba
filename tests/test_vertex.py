"""Tests for core.vertex — Vertex AI auth / project resolution."""

import json
import os
from pathlib import Path

import pytest

import core.vertex as vertex


# ---------------------------------------------------------------------------
# vertex_project()
# ---------------------------------------------------------------------------

def test_vertex_project_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-gcp-project")
    monkeypatch.setattr(vertex, "service_account_path", lambda: None)
    assert vertex.vertex_project() == "my-gcp-project"


def test_vertex_project_from_sa_json(monkeypatch, tmp_path):
    sa_file = tmp_path / "roba.json"
    sa_file.write_text(json.dumps({"project_id": "from-json-project"}))
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(vertex, "service_account_path", lambda: sa_file)
    assert vertex.vertex_project() == "from-json-project"


def test_vertex_project_env_wins_over_sa_json(monkeypatch, tmp_path):
    sa_file = tmp_path / "roba.json"
    sa_file.write_text(json.dumps({"project_id": "from-json"}))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")
    monkeypatch.setattr(vertex, "service_account_path", lambda: sa_file)
    assert vertex.vertex_project() == "from-env"


def test_vertex_project_empty_without_env_or_file(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(vertex, "service_account_path", lambda: None)
    assert vertex.vertex_project() == ""


# ---------------------------------------------------------------------------
# vertex_location()
# ---------------------------------------------------------------------------

def test_vertex_location_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    assert vertex.vertex_location() == "us-central1"


def test_vertex_location_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west1")
    assert vertex.vertex_location() == "europe-west1"


# ---------------------------------------------------------------------------
# vertex_available()
# ---------------------------------------------------------------------------

def test_vertex_available_true_with_project(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setattr(vertex, "service_account_path", lambda: None)
    assert vertex.vertex_available() is True


def test_vertex_available_false_without_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(vertex, "service_account_path", lambda: None)
    assert vertex.vertex_available() is False


# ---------------------------------------------------------------------------
# service_account_path()
# ---------------------------------------------------------------------------

def test_sa_path_from_env(monkeypatch, tmp_path):
    sa_file = tmp_path / "key.json"
    sa_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(sa_file))
    result = vertex.service_account_path()
    assert result == sa_file


def test_sa_path_none_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "nonexistent.json"))
    assert vertex.service_account_path() is None


def test_sa_path_default_roba_json_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    # The default roba.json won't exist in test context — expect None.
    # (Unless the repo root happens to have one, which is fine to skip.)
    result = vertex.service_account_path()
    assert result is None or result.name == "roba.json"
