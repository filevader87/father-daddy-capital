"""
Health Check Endpoint
--------------------
Simple health check for monitoring and load balancers
"""

from fastapi import FastAPI
from datetime import datetime
import psutil
import logging

logger = logging.getLogger(__name__)

app = FastAPI(title="Father Daddy Capital Health Check")

@app.get("/healthz")
async def health_check():
    """Health check endpoint."""
    try:
        # Basic system health checks
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Check if system is healthy
        healthy = (
            cpu_percent < 90 and
            memory.percent < 90 and
            disk.percent < 90
        )
        
        status = "healthy" if healthy else "unhealthy"
        
        return {
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
            "system": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "disk_percent": disk.percent
            }
        }
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "timestamp": datetime.utcnow().isoformat(),
            "error": str(e)
        }

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "Father Daddy Capital",
        "version": "2.0.0",
        "status": "running",
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)