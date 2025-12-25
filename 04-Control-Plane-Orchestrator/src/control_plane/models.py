"""
Control Plane Data Models

Defines the canonical Job and JobExecution models for the Control Plane.
These models are the source of truth for job state in the database.
"""
from sqlmodel import SQLModel, Field, Column, JSON
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum as PyEnum


class JobStatus(str, PyEnum):
    """Job status enumeration - matches job_orchestrator.py JobStatus."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(SQLModel, table=True):
    """
    Canonical Job model for Control Plane.
    
    This is the source of truth for job state in the database.
    Fields match what job_orchestrator.py expects.
    """
    __tablename__ = "jobs"
    
    id: str = Field(primary_key=True, description="UUID job identifier")
    domain: str = Field(index=True, description="Target domain (e.g., 'amazon.com')")
    url: str = Field(description="Target URL for the job")
    job_type: str = Field(index=True, description="Job type (e.g., 'navigate_extract', 'authenticate')")
    strategy: str = Field(default="vanilla", description="Execution strategy (vanilla, stealth, assault)")
    payload: str = Field(description="JSON-encoded job payload")
    priority: int = Field(default=2, index=True, description="Priority: 0=emergency, 1=high, 2=normal, 3=low")
    status: JobStatus = Field(default=JobStatus.PENDING, index=True, description="Current job status")
    max_attempts: int = Field(default=3, description="Maximum retry attempts")
    attempts: int = Field(default=0, description="Current attempt number")
    timeout_seconds: int = Field(default=300, description="Job timeout in seconds")
    result: Optional[str] = Field(default=None, description="JSON-encoded result data")
    artifacts: Optional[str] = Field(default=None, description="JSON-encoded artifact references")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    
    class Config:
        arbitrary_types_allowed = True


class JobExecution(SQLModel, table=True):
    """
    Job execution history record.
    
    Tracks each execution attempt for a job, including retries.
    """
    __tablename__ = "job_executions"
    
    id: str = Field(primary_key=True, description="UUID execution identifier")
    job_id: str = Field(index=True, description="Reference to Job.id")
    attempt: int = Field(description="Attempt number (1, 2, 3, ...)")
    status: str = Field(description="Execution status: 'success' or 'failed'")
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(default=None)
    execution_time_ms: Optional[int] = Field(default=None, description="Execution duration in milliseconds")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    
    class Config:
        arbitrary_types_allowed = True

