from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _create_engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True)


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def ensure_database_exists() -> None:
    """在 MySQL 中创建 DATABASE_URL 指向的数据库；账户必须拥有建库权限。"""
    database_url = make_url(get_settings().database_url)
    if database_url.get_backend_name() != "mysql" or not database_url.database:
        return

    # 连接到服务端而非目标库，避免目标库尚未创建时无法建立连接。
    server_engine = create_engine(database_url.set(database=None), pool_pre_ping=True)
    try:
        with server_engine.begin() as connection:
            database_name = connection.dialect.identifier_preparer.quote(database_url.database)
            connection.execute(text(
                f"CREATE DATABASE IF NOT EXISTS {database_name} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            ))
    finally:
        server_engine.dispose()


def ensure_database_schema() -> None:
    """确保数据库存在，并补齐当前 SQLAlchemy 元数据中缺失的表。"""
    ensure_database_exists()
    Base.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
