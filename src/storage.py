"""
SQLite simples para rastrear jobs já vistos e evitar notificações duplicadas.
"""
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, DateTime, select
from datetime import datetime

DB_PATH = Path(__file__).parent.parent / "workana.db"
_engine = None
_factory = None


class Base(DeclarativeBase):
    pass


class SeenJob(Base):
    __tablename__ = "seen_jobs"
    slug: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, default="")
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def _get_factory() -> async_sessionmaker[AsyncSession]:
    global _engine, _factory
    if _engine is None:
        _engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", echo=False)
        _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _factory


async def init_db():
    _get_factory()  # ensure engine is created
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_seen_slugs() -> set[str]:
    async with _get_factory()() as session:
        result = await session.execute(select(SeenJob.slug))
        return set(result.scalars().all())


async def mark_seen(slug: str, title: str = ""):
    async with _get_factory()() as session:
        existing = await session.get(SeenJob, slug)
        if not existing:
            session.add(SeenJob(slug=slug, title=title, seen_at=datetime.utcnow()))
            await session.commit()
