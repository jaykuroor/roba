"""Shared pytest fixtures for the core test suite."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.db as db
from core.bus import SignalBus


@pytest.fixture
def session_factory():
    """A real in-memory SQLite session factory with all tables created."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def bus(session_factory):
    return SignalBus(session_factory)
