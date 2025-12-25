"""
Control Plane Configuration

Settings and configuration management.
"""
import os
from typing import Optional
from pydantic_settings import BaseSettings


class ControlPlaneSettings(BaseSettings):
    """Control Plane configuration settings."""
    
    # Database
    database_url: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/accord_engine")
    
    # Redis
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # API
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8080"))
    
    # Job processing
    max_concurrent_jobs: int = int(os.getenv("MAX_CONCURRENT_JOBS", "100"))
    worker_count: int = int(os.getenv("WORKER_COUNT", "3"))
    
    @property
    def postgres_dsn(self) -> str:
        """Get PostgreSQL DSN for asyncpg."""
        # Convert postgresql:// to postgresql+asyncpg:// if needed
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return self.database_url
    
    class Config:
        env_file = ".env"
        case_sensitive = False

