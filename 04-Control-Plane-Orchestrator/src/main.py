"""
Control Plane API

FastAPI application for job orchestration and management.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from .config import ControlPlaneSettings
from .database import Database
from .control_plane.job_orchestrator import JobOrchestrator
from .control_plane.models import JobStatus, Job

# Execution Engine imports (optional - will fail gracefully if not available)
try:
    import sys
    import os
    execution_engine_path = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "01-Core-Execution-Engine", "src"
    )
    if execution_engine_path not in sys.path:
        sys.path.insert(0, execution_engine_path)
    
    from core.browser_pool import BrowserPool
    EXECUTION_ENGINE_AVAILABLE = True
except ImportError:
    # Logger not yet initialized, use print or structlog
    import structlog
    _log = structlog.get_logger(__name__)
    _log.warning("Execution Engine not available - browser automation disabled")
    BrowserPool = None
    EXECUTION_ENGINE_AVAILABLE = False


def setup_logging() -> None:
    """Configure structured logging."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    logging.basicConfig(level=logging.INFO)


# Initialize settings and logging
settings = ControlPlaneSettings()
setup_logging()
logger = structlog.get_logger(__name__)

# Initialize connections
redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
db = Database(settings)

# Initialize browser pool (if Execution Engine available)
browser_pool: BrowserPool | None = None

# Initialize orchestrator (will be created in lifespan)
orchestrator: JobOrchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan: startup and shutdown.
    
    - Initialize database tables
    - Create orchestrator
    - Start workers
    - Cleanup on shutdown
    """
    global orchestrator
    
    # Startup
    logger.info("control_plane_starting")
    
    # Initialize database (with error handling and timeout)
    try:
        import asyncio
        await asyncio.wait_for(db.init_models(), timeout=5.0)
        logger.info("database_initialized")
    except asyncio.TimeoutError:
        logger.warning("database_init_timeout - continuing without database initialization")
    except Exception as e:
        logger.error("database_init_failed", error=str(e), error_type=type(e).__name__)
        # Continue anyway - database might be temporarily unavailable
        # The app can still serve requests, but job operations will fail
    
    # Initialize browser pool (if Execution Engine available)
    global browser_pool
    if EXECUTION_ENGINE_AVAILABLE and BrowserPool:
        browser_pool = BrowserPool(max_instances=20, max_pages_per_instance=5)
        await browser_pool.initialize()
        logger.info("browser_pool_initialized")
    else:
        logger.warning("browser_pool_not_available")
    
    # Create orchestrator
    orchestrator = JobOrchestrator(
        redis_client=redis_client,
        db=db,  # Pass Database instance (not just engine)
        browser_pool=browser_pool,
        db_session=db.session(),  # AsyncSession for Execution Engine
        max_concurrent_jobs=settings.max_concurrent_jobs,
    )
    
    # NOTE: Workers are disabled in containerized deployments
    # The Execution Engine worker service handles job execution via Redis Streams
    # Control Plane only enqueues jobs, does not process them
    # 
    # If you need Control Plane to process jobs (local dev without Execution Engine container):
    # Uncomment the following lines:
    #
    # for i in range(settings.worker_count):
    #     worker_id = f"worker-{i+1}"
    #     task = asyncio.create_task(orchestrator.start_worker(worker_id))
    #     orchestrator._workers.append(task)
    #     logger.info("worker_started", worker_id=worker_id)
    
    logger.info("control_plane_ready", workers=0, note="Execution Engine worker handles job processing")
    
    yield
    
    # Shutdown
    logger.info("control_plane_shutting_down")
    if orchestrator:
        await orchestrator.shutdown()
    
    # Cleanup browser pool
    if browser_pool and hasattr(browser_pool, 'playwright') and browser_pool.playwright:
        await browser_pool.playwright.stop()
        logger.info("browser_pool_stopped")
    
    await db.dispose()
    await redis_client.aclose()
    logger.info("control_plane_stopped")


# Create FastAPI app
app = FastAPI(
    title="Control Plane Orchestrator API",
    description="""
    Job orchestration and management API for Accord Engine.
    
    ## Features
    
    * **Job Management**: Create, monitor, and cancel jobs
    * **Queue Management**: Priority-based job queuing with Redis Streams
    * **Idempotency**: Prevent duplicate job creation
    * **Status Tracking**: Real-time job status and progress monitoring
    
    ## Authentication
    
    Currently authentication is disabled for development. In production, use API keys or JWT tokens.
    """,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


def get_orchestrator() -> JobOrchestrator:
    """Dependency to get orchestrator instance."""
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator not initialized"
        )
    return orchestrator


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "control-plane",
        "workers": settings.worker_count,
    }


# Job creation endpoint
@app.post("/api/v1/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(
    domain: str,
    url: str,
    job_type: str,
    strategy: str = "vanilla",
    priority: int = 2,
    payload: dict = {},
    idempotency_key: str | None = None,
    timeout_seconds: int = 300,
    orch: JobOrchestrator = Depends(get_orchestrator),
):
    """
    Create a new job.
    
    Args:
        domain: Target domain (e.g., 'amazon.com')
        url: Target URL
        job_type: Type of job ('navigate_extract', 'authenticate', etc.)
        strategy: Execution strategy ('vanilla', 'stealth', 'assault')
        priority: Priority level (0=emergency, 1=high, 2=normal, 3=low)
        payload: Job-specific payload data
        idempotency_key: Optional idempotency key to prevent duplicates
        timeout_seconds: Job timeout in seconds
        
    Returns:
        Job ID and status
    """
    try:
        job_id = await orch.create_job(
            domain=domain,
            url=url,
            job_type=job_type,
            strategy=strategy,
            payload=payload,
            priority=priority,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
        )
        
        return {
            "job_id": job_id,
            "status": "created",
            "domain": domain,
            "job_type": job_type,
        }
        
    except Exception as e:
        logger.error("job_creation_failed", error=str(e), domain=domain)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create job: {str(e)}"
        )


# Job status endpoint
@app.get("/api/v1/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    orch: JobOrchestrator = Depends(get_orchestrator),
):
    """
    Get job status and details.
    
    Args:
        job_id: The job ID
        
    Returns:
        Job status and details
    """
    status_info = await orch.get_job_status(job_id)
    
    if status_info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )
    
    return status_info


# Queue stats endpoint
@app.get("/api/v1/queue/stats")
async def get_queue_stats(
    orch: JobOrchestrator = Depends(get_orchestrator),
):
    """Get queue statistics."""
    stats = await orch.get_queue_stats()
    return stats


# Operator Dashboard endpoint
@app.get("/api/v1/ops/status")
async def get_ops_status(
    orch: JobOrchestrator = Depends(get_orchestrator),
):
    """
    Operator Dashboard: Health, queue depth, recent jobs, and success rate.
    
    Returns:
        - health: Service health status
        - queue_depth: Current number of jobs in queue
        - recent_jobs: List of recent jobs (last 10)
        - success_rate: Success rate percentage (last 100 jobs)
    """
    from sqlmodel import select, desc
    from datetime import datetime
    
    try:
        # Get queue depth
        queue_depth = await orch.get_queue_depth()
    except Exception as e:
        logger.error("ops_status_queue_depth_error", error=str(e))
        queue_depth = 0
    
    # Get recent jobs (last 10)
    recent_jobs = []
    try:
        async with db.session() as session:
            statement = (
                select(Job)
                .order_by(desc(Job.created_at))
                .limit(10)
            )
            result = await session.execute(statement)
            jobs = result.scalars().all()
            
            for job in jobs:
                # Handle enum status
                status_value = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
                recent_jobs.append({
                    "job_id": job.id,
                    "domain": job.domain,
                    "status": status_value,
                    "job_type": job.job_type,
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                })
    except Exception as e:
        logger.error("ops_status_recent_jobs_error", error=str(e))
        # Continue with empty list
    
    # Calculate success rate (last 100 jobs)
    success_rate = 0.0
    try:
        async with db.session() as session:
            # Get last 100 completed jobs
            statement = (
                select(Job)
                .where(Job.completed_at.isnot(None))
                .order_by(desc(Job.completed_at))
                .limit(100)
            )
            result = await session.execute(statement)
            completed_jobs = result.scalars().all()
            
            if completed_jobs:
                # Handle both enum and string status values
                successful = sum(
                    1 for job in completed_jobs 
                    if (job.status == JobStatus.COMPLETED if isinstance(job.status, JobStatus) else str(job.status) == "completed")
                )
                success_rate = (successful / len(completed_jobs)) * 100.0
    except Exception as e:
        logger.error("ops_status_success_rate_error", error=str(e))
        # Continue with 0.0
    
    # Get overall health
    health = "healthy"
    if queue_depth > 1000:
        health = "degraded"
    if queue_depth > 5000:
        health = "unhealthy"
    
    return {
        "health": health,
        "queue_depth": queue_depth,
        "recent_jobs": recent_jobs,
        "success_rate": round(success_rate, 2),
        "timestamp": datetime.utcnow().isoformat(),
    }


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "control-plane",
        "version": "1.0.0",
        "status": "operational",
    }


# For running directly with python -m
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
