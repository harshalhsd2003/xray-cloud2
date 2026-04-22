import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./local.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class Detection(Base):
    __tablename__ = "detections"
    id          = Column(Integer, primary_key=True, index=True)
    timestamp   = Column(DateTime, default=datetime.utcnow, index=True)
    date_dmy    = Column(String(20))
    time_hms    = Column(String(20))
    class_name  = Column(String(100), index=True)
    filename    = Column(String(255))
    image_url   = Column(Text, nullable=True)   # Cloudinary URL
    conf        = Column(Float, nullable=True)
    camera_idx  = Column(Integer, default=0)

class SystemSettings(Base):
    __tablename__ = "system_settings"
    id                   = Column(Integer, primary_key=True)
    confidence_threshold = Column(Float, default=0.80)
    confirm_frames       = Column(Integer, default=12)
    camera_index         = Column(Integer, default=0)
    updated_at           = Column(DateTime, default=datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # seed default settings row
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        result = await session.execute(select(SystemSettings).limit(1))
        if not result.scalar():
            session.add(SystemSettings())
            await session.commit()

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
