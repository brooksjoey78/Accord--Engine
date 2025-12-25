"""
Control Plane Database

Database connection and session management.
Follows the same pattern as Memory Service.
"""
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
import structlog
import os

from .config import ControlPlaneSettings
from .control_plane.models import Job, JobExecution

logger = structlog.get_logger(__name__)


class Database:
    """
    Database connection manager for Control Plane.
    
    Uses async SQLModel with asyncpg, following Memory Service pattern.
    """
    
    def __init__(self, settings: ControlPlaneSettings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_async_engine(
            settings.postgres_dsn,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=15,
            echo=False,
        )
        self._session_factory = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    
    @property
    def engine(self) -> AsyncEngine:
        """Get the database engine."""
        return self._engine
    
    def session(self) -> AsyncSession:
        """Get a database session."""
        return self._session_factory()
    
    async def init_models(self) -> None:
        """
        Initialize database tables.
        
        Creates all tables defined in SQLModel models.
        
        NOTE: This is for development/quickstart only.
        In production, use Alembic migrations instead.
        Set SKIP_INIT_MODELS=true to skip this in production.
        """
        # Allow skipping in production (use migrations instead)
        if os.getenv("SKIP_INIT_MODELS", "false").lower() == "true":
            logger.info("Skipping init_models (migrations should be used in production)")
            return
        
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        logger.info("control_plane_tables_initialized (dev mode - use migrations in production)")
    
    async def dispose(self) -> None:
        """Close database connections."""
        await self._engine.dispose()

