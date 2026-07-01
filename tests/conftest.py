"""
Test configuration and fixtures.

Strategy:
  - Uses a REAL PostgreSQL database (job_queue_test) running via Docker.
  - Uses a REAL Redis instance (DB index 1, separate from production DB 0).
  - Each test gets a clean slate: all tables are truncated and Redis is flushed
    before every test, so tests are fully isolated from each other.

To run tests:
    docker-compose up postgres redis -d
    pytest tests/ -v
"""
import logging
import os
import pytest

logging.basicConfig(
    level=logging.INFO,
    format='{"level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}',
)
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.api.deps import get_db
from app.main import app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5433/job_queue_test",
)

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/1")

# ---------------------------------------------------------------------------
# Create test database if it doesn't exist
# ---------------------------------------------------------------------------

def _ensure_test_database():
    """
    Connect to the default 'postgres' DB and create job_queue_test if missing.
    This runs once before any tests.
    """
    # Build the admin URL by replacing the DB name with 'postgres'
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = 'job_queue_test'")
        ).fetchone()
        if not exists:
            conn.execute(text("CREATE DATABASE job_queue_test"))
            print("\nCreated test database: job_queue_test")
    admin_engine.dispose()

_ensure_test_database()

# ---------------------------------------------------------------------------
# Engine + session for test DB
# ---------------------------------------------------------------------------

engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture(scope="session", autouse=True)
def create_tables():
    """Create all tables once per test session, drop them at the end."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def clean_database():
    """Truncate all tables before each test for a clean slate."""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f"TRUNCATE TABLE {table.name} RESTART IDENTITY CASCADE"))


# ---------------------------------------------------------------------------
# Redis — DB index 1, isolated from production
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_redis():
    """Flush the test Redis DB before each test."""
    import redis
    r = redis.from_url(TEST_REDIS_URL, decode_responses=True)
    r.flushdb()
    yield
    r.flushdb()


# ---------------------------------------------------------------------------
# DB session fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    """Yield a test database session."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------

@pytest.fixture
def client(db):
    """
    FastAPI test client wired to the real test database and real Redis (DB 1).
    """
    def override_get_db():
        try:
            yield db
        finally:
            pass

    # Point the Redis client at test DB 1
    import redis
    import app.queue.redis_client as queue_module
    queue_module._redis_client = redis.from_url(TEST_REDIS_URL, decode_responses=True)

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    queue_module._redis_client = None
