import psycopg
from psycopg.rows import dict_row

from eventindex import config


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or config.DATABASE_URL, row_factory=dict_row)
