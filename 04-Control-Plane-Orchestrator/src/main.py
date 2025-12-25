"""
Control Plane Orchestrator - Main FastAPI Application
Entry point for Cloud Run deployment
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import os
import sys

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(
    title="Accord Engine Control Plane",
    description="Policy-driven web automation platform - Control Plane API",
    version="1.0.0"
)

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "accord-control-plane",
        "status": "running",
        "version": "1.0.0"
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "control-plane"
    }

# Import and mount routers if they exist
try:
    from control_plane import job_orchestrator, state_manager
    # Add routes here when modules are available
except ImportError as e:
    # Graceful degradation - app will still run
    print(f"Warning: Could not import some modules: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)

