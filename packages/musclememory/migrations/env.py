from alembic import context
from musclememory.storage import Base
from sqlalchemy import engine_from_config, pool

config = context.config
target_metadata = Base.metadata


def run_migrations():
    if context.is_offline_mode():
        context.configure(
            url=config.get_main_option("sqlalchemy.url"),
            target_metadata=target_metadata,
            literal_binds=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = engine_from_config(
            config.get_section(config.config_ini_section),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                version_table_schema=config.get_main_option("version_table_schema") or None,
            )
            with context.begin_transaction():
                context.run_migrations()


run_migrations()
