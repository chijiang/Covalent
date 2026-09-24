from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def migration_config(database_url: str) -> Config:
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")
    database_url = url.render_as_string(hide_password=False)
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["explicit_database_url"] = True
    return config


def run_database_migrations(database_url: str) -> None:
    command.upgrade(migration_config(database_url), "head")
