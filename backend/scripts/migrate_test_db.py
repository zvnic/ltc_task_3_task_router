"""Bring the configured test database to Alembic head without exposing its URL."""

import asyncio
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine


async def table_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return set(
                await connection.run_sync(
                    lambda sync_connection: inspect(sync_connection).get_table_names()
                )
            )
    finally:
        await engine.dispose()


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("TEST_DATABASE_URL is required")
    tables = asyncio.run(table_names(database_url))
    os.environ["DATABASE_URL"] = database_url
    config = Config("alembic.ini")
    if "datasets" in tables and "alembic_version" not in tables:
        command.stamp(config, "0001_initial")
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
