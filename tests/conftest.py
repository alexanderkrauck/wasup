import psycopg
import pytest
from psycopg.rows import dict_row

from eventindex import config
from eventindex.db.migrate import migrate

TEST_DB = "eventindex_test"
TEST_URL = config.DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DB


@pytest.fixture(scope="session")
def test_db_url() -> str:
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        exists = c.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
        ).fetchone()
        if not exists:
            c.execute(f"CREATE DATABASE {TEST_DB}")
    with psycopg.connect(TEST_URL, row_factory=dict_row) as conn:
        migrate(conn)
    # everything in the test process (incl. record_spend's own connections)
    # must hit the test db
    config.DATABASE_URL = TEST_URL
    return TEST_URL


@pytest.fixture
def conn(test_db_url):
    with psycopg.connect(test_db_url, row_factory=dict_row) as c:
        yield c
        c.rollback()
        c.execute(
            "TRUNCATE jobs, crawl_log, budget_spend, event_claim, occurrence, "
            "identity, event, source, venue, report, api_key, text_recurrence, "
            "enrichment, adjudication, probe_rejection, event_tag, "
            "tag_embedding CASCADE"
        )
        c.commit()
