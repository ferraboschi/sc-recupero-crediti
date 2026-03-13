"""FastAPI application entry point for SC Recupero Crediti."""

import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from backend.database import init_db, get_engine
from backend.config import config
from backend.scheduler import start_scheduler, stop_scheduler
from backend.api import dashboard, positions, messages, customers, sync, webhooks, recovery, system

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="SC Recupero Crediti API",
    description="API for debt recovery management system",
    version="1.0.0"
)

# CORS middleware - allow frontend origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_migrations():
    """Run lightweight schema migrations for new columns."""
    try:
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            # Add 'outcome' column to recovery_actions if missing
            try:
                conn.execute(text('SELECT outcome FROM recovery_actions LIMIT 1'))
                logger.info("Migration: 'outcome' column already exists")
            except Exception:
                conn.rollback()
                conn.execute(text('ALTER TABLE recovery_actions ADD COLUMN outcome VARCHAR'))
                conn.commit()
                logger.info("Migration: added 'outcome' column to recovery_actions")
    except Exception as e:
        logger.warning(f"Migration warning (non-fatal): {e}")


# Ultra-lightweight health check for Render (must respond < 1s)
@app.get("/health")
@app.get("/api/health")
async def health_check():
    """Health check endpoint — kept minimal to avoid Render timeout."""
    return {"status": "ok"}


# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize database and start scheduler on app startup."""
    for attempt in range(3):
        try:
            logger.info(f"Initializing database (attempt {attempt + 1})...")
            init_db()
            # Run lightweight migrations for new columns
            _run_migrations()
            logger.info("Database initialized successfully")
            break
        except Exception as e:
            logger.error(f"Failed to initialize database (attempt {attempt + 1}): {e}")
            if attempt < 2:
                import asyncio
                await asyncio.sleep(2)
            else:
                raise

    try:
        logger.info("Starting scheduler...")
        start_scheduler()
        logger.info("Scheduler started successfully")
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")
        # Don't raise - scheduler failure shouldn't prevent app startup


# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Stop scheduler on app shutdown."""
    try:
        logger.info("Stopping scheduler...")
        stop_scheduler()
        logger.info("Scheduler stopped")
    except Exception as e:
        logger.error(f"Error stopping scheduler: {e}")


# Include routers
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(positions.router, prefix="/api/positions", tags=["positions"])
app.include_router(messages.router, prefix="/api/messages", tags=["messages"])
app.include_router(customers.router, prefix="/api/customers", tags=["customers"])
app.include_router(sync.router, prefix="/api/sync", tags=["sync"])
app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(recovery.router, prefix="/api/recovery", tags=["recovery"])
app.include_router(system.router, prefix="/api/system", tags=["system"])


# Mount static files for frontend (if they exist)
frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
    logger.info(f"Frontend static files mounted from {frontend_dist}")
else:
    logger.warning(f"Frontend dist directory not found at {frontend_dist}")


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for unhandled errors."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
