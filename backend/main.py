"""FastAPI application entry point for SC Recupero Crediti."""

import logging
from pathlib import Path
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from backend.database import init_db
from backend.config import config
from backend.scheduler import start_scheduler, stop_scheduler
from backend.api import auth, dashboard, positions, customers, sync, recovery, system, avvocato
from backend.api.auth import verify_token

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


# Ultra-lightweight health check for Render (must respond < 1s)
@app.get("/health")
@app.get("/api/health")
async def health_check():
    """Health check endpoint — kept minimal to avoid Render timeout."""
    return {"status": "ok"}


# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize database and start scheduler on app startup.

    Runs DB init in a background thread so it does not block the
    event loop — the /health endpoint can respond immediately while
    the database connects (critical for Render's 5-second window).
    """
    import threading

    def _background_init():
        for attempt in range(3):
            try:
                logger.info(
                    f"Initializing database "
                    f"(attempt {attempt + 1})..."
                )
                init_db()
                logger.info("Database initialized successfully")
                break
            except Exception as e:
                logger.error(
                    f"Failed to initialize database "
                    f"(attempt {attempt + 1}): {e}"
                )
                if attempt < 2:
                    import time
                    time.sleep(2)

        try:
            # Backfill una-tantum delle pratiche (idempotente: marker in
            # sync_state scritto nello stesso commit, retry al prossimo
            # avvio in caso di fallimento).
            from backend.engine.cases import run_backfill_if_needed
            run_backfill_if_needed()
        except Exception as e:
            logger.error(f"Case backfill error: {e}")

        try:
            # Backfill una-tantum della tabella di join azione↔fattura
            # (solleciti per-fattura). Idempotente: marker in sync_state,
            # retry al prossimo avvio. DOPO il case-backfill: le azioni hanno
            # già la loro pratica/fatture agganciate.
            from backend.engine.action_invoices import (
                run_backfill_action_invoices_if_needed,
            )
            run_backfill_action_invoices_if_needed()
        except Exception as e:
            logger.error(f"Action-invoices backfill error: {e}")

        try:
            # Una-tantum: ridistribuzione degli stati cliente col rollup
            # per-fattura, registrata in ActivityLog (status_resplit).
            from backend.engine.cases import resplit_status_if_needed
            resplit_status_if_needed()
        except Exception as e:
            logger.error(f"Status resplit error: {e}")

        try:
            # Backfill una-tantum dello storico STIMATO dello scaduto
            # (stesso pattern: marker one-shot in sync_state, retry al
            # prossimo avvio). DOPO il case-backfill: il grafico evoluzione
            # parte popolato invece che vuoto.
            from backend.engine.overdue_history import (
                run_history_backfill_if_needed,
            )
            run_history_backfill_if_needed()
        except Exception as e:
            logger.error(f"Overdue history backfill error: {e}")

        # NB: il repair degli abbinamenti NON gira più allo startup — è uno
        # step del full sync (_full_sync_task), così vede le P.IVA reali già
        # popolate dall'anagrafica invece di girare a vuoto sul boot.

        try:
            logger.info("Starting scheduler...")
            start_scheduler()
            logger.info("Scheduler started successfully")
        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}")

    t = threading.Thread(target=_background_init, daemon=True)
    t.start()


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
# Auth — public (no token required)
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])

# Protected routes — all require JWT token
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(verify_token)])
app.include_router(positions.router, prefix="/api/positions", tags=["positions"], dependencies=[Depends(verify_token)])
app.include_router(customers.router, prefix="/api/customers", tags=["customers"], dependencies=[Depends(verify_token)])
app.include_router(sync.router, prefix="/api/sync", tags=["sync"], dependencies=[Depends(verify_token)])
app.include_router(recovery.router, prefix="/api/recovery", tags=["recovery"], dependencies=[Depends(verify_token)])
app.include_router(system.router, prefix="/api/system", tags=["system"], dependencies=[Depends(verify_token)])
app.include_router(avvocato.router, prefix="/api/avvocato", tags=["avvocato"], dependencies=[Depends(verify_token)])


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
