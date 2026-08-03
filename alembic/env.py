"""Alembic environment for KnowFlow's async MySQL schema."""

from __future__ import annotations

import asyncio
import importlib
import os
from logging.config import fileConfig
from types import ModuleType

from alembic import context
from sqlalchemy import MetaData, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

MODEL_MODULES = (
    "knowflow.infrastructure.db.models.identity",
    "knowflow.infrastructure.db.models.workflow",
    "knowflow.infrastructure.db.models.ticketing",
    "knowflow.infrastructure.db.models.knowledge",
)


def _import_if_available(module_name: str) -> ModuleType | None:
    """Import model modules as they are added without masking their import errors."""

    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise


def _load_target_metadata() -> MetaData | None:
    for module_name in MODEL_MODULES:
        _import_if_available(module_name)

    candidates = (
        "knowflow.infrastructure.db.models",
        "knowflow.infrastructure.db.session",
    )
    for module_name in candidates:
        module = _import_if_available(module_name)
        if module is None:
            continue
        base = getattr(module, "Base", None)
        metadata = getattr(base, "metadata", None)
        if isinstance(metadata, MetaData):
            return metadata
        metadata = getattr(module, "metadata", None)
        if isinstance(metadata, MetaData):
            return metadata
    return None


target_metadata = _load_target_metadata()


def _database_url() -> str:
    return os.environ.get("KNOWFLOW_DATABASE_URL") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Emit SQL without creating an Engine."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_sync_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations through SQLAlchemy's asyncio bridge."""

    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
