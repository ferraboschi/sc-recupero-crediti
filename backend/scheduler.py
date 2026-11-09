"""APScheduler integration for scheduled jobs."""

import logging
from datetime import datetime, timedelta
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from backend.config import config

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional[BackgroundScheduler] = None
_scheduler_started = False

# Track last sync times
_last_sync = {
    "invoices": None,
    "customers": None,
    "matching": None,
    "cases": None,
}


def run_daily_job(manual: bool = False):
    """
    Daily job that runs the full sequential sync.

    Pipeline COMPLETA: invoices → customers → repair → matching →
    auto-create → case lifecycle → order matching. Include l'aggancio
    ordini Shopify (il passo più lento/rate-limited), che gira così una
    volta al giorno + a ogni deploy (startup). I solleciti sono manuali
    (Copia Messaggio): nessun invio automatico.

    Args:
        manual: Whether this is a manual trigger (vs scheduled)

    Returns:
        Dictionary with job results
    """
    from backend.api.sync import _full_sync_task

    logger.info(f"Starting daily job (manual={manual})")

    try:
        results = _full_sync_task(include_order_matching=True, manual=manual)
        results["timestamp"] = datetime.utcnow().isoformat()
    except Exception as e:
        logger.error(f"Error in daily job: {e}", exc_info=True)
        results = {"error": str(e), "timestamp": datetime.utcnow().isoformat()}

    # Update last sync times
    now = datetime.utcnow().isoformat()
    for key in _last_sync:
        _last_sync[key] = now

    logger.info(f"Daily job completed: {results}")
    return results


def run_hourly_job():
    """
    Hourly job: sync LEGGERO senza aggancio ordini Shopify.

    Pipeline: invoices → customers → repair → matching → auto-create →
    case lifecycle. Salta l'aggancio ordini (include_order_matching=False),
    il passo più lento e rate-limited: fatture, clienti, abbinamenti e
    pratiche restano freschi ogni ora, l'aggancio ordini lo fa il job
    giornaliero. Il _sync_lock impedisce sovrapposizioni con altri sync.

    Returns:
        Dictionary with job results
    """
    from backend.api.sync import _full_sync_task

    # Cede la precedenza al giornaliero quando gli slot coincidono: se il
    # daily è configurato al minuto 0 (stesso istante dell'orario) e siamo in
    # quell'ora, salta — così il _sync_lock non-bloccante non rischia di far
    # scartare il full sync (con aggancio ordini) in favore di quello leggero.
    now_local = datetime.now(pytz.timezone(config.TIMEZONE))
    if config.SCHEDULER_MINUTE == 0 and now_local.hour == config.SCHEDULER_HOUR:
        logger.info("Hourly job skipped: coincide con lo slot del daily full sync")
        return {"skipped": "coincides with daily full sync",
                "timestamp": datetime.utcnow().isoformat()}

    logger.info("Starting hourly job (light sync, no order matching)")

    try:
        results = _full_sync_task(include_order_matching=False, manual=False)
        results["timestamp"] = datetime.utcnow().isoformat()
    except Exception as e:
        logger.error(f"Error in hourly job: {e}", exc_info=True)
        results = {"error": str(e), "timestamp": datetime.utcnow().isoformat()}

    # Update last sync times
    now = datetime.utcnow().isoformat()
    for key in _last_sync:
        _last_sync[key] = now

    logger.info(f"Hourly job completed: {results}")
    return results


def run_reminder_email_job():
    """Promemoria email il giorno prima della scadenza (una volta al giorno,
    dopo il sync completo delle 08:30 così lo stato delle fatture è fresco).
    In modalità prova compone e registra soltanto."""
    from backend.database import get_session_direct
    from backend.engine.reminder_email import run_reminders

    from backend.engine.reminder_email import ReminderBusy

    session = get_session_direct()
    try:
        summary = run_reminders(session)
        brief = {k: v for k, v in summary.items() if k != "items"}
        logger.info(f"Reminder email job completed: {brief}")
        return summary
    except ReminderBusy:
        logger.warning("Reminder email job skipped: un giro è già in corso")
        return {"skipped": "busy"}
    except Exception as e:
        logger.error(f"Reminder email job failed: {e}", exc_info=True)
        return {"error": str(e)}
    finally:
        try:
            session.close()
        except Exception:
            pass


def run_reminder_email_catchup():
    """Dopo un riavvio (deploy, cold start) recupera il giro delle 09:15 se è
    saltato, ma solo in orario d'ufficio italiano: idempotente per
    costruzione (le righe già lavorate non si rifanno)."""
    from backend.engine.reminder_email import in_catchup_window

    if not in_catchup_window():
        logger.info("Reminder email catch-up skipped (fuori dalla finestra 09-18)")
        return {"skipped": "window"}
    return run_reminder_email_job()


def start_scheduler():
    """Start the background scheduler."""
    global _scheduler, _scheduler_started

    if _scheduler_started:
        logger.warning("Scheduler already started")
        return

    _scheduler = BackgroundScheduler(
        timezone=pytz.timezone(config.TIMEZONE)
    )

    # Schedule daily job at configured time
    try:
        trigger = CronTrigger(
            hour=config.SCHEDULER_HOUR,
            minute=config.SCHEDULER_MINUTE,
            timezone=config.TIMEZONE
        )
        _scheduler.add_job(
            run_daily_job,
            trigger=trigger,
            id="daily_sync_job",
            name="Daily invoice sync",
            replace_existing=True,
            max_instances=1,  # Prevent concurrent execution
        )
        logger.info(
            f"Scheduled daily job at {config.SCHEDULER_HOUR:02d}:{config.SCHEDULER_MINUTE:02d} "
            f"({config.TIMEZONE})"
        )
    except Exception as e:
        logger.error(f"Failed to schedule daily job: {e}", exc_info=True)
        raise

    # Schedule hourly light sync (allo scoccare di ogni ora): sync leggero
    # SENZA aggancio ordini. Tiene fatture/clienti/pratiche freschi ogni ora
    # senza il passo Shopify lento; il _sync_lock evita sovrapposizioni col
    # giornaliero (che a 8:30 fa il sync completo).
    try:
        hourly_trigger = CronTrigger(minute=0, timezone=config.TIMEZONE)
        _scheduler.add_job(
            run_hourly_job,
            trigger=hourly_trigger,
            id="hourly_sync_job",
            name="Hourly light sync (no order matching)",
            replace_existing=True,
            max_instances=1,  # Skip if a previous run is still going
        )
        logger.info(f"Scheduled hourly light sync (minute=0, every hour, {config.TIMEZONE})")
    except Exception as e:
        logger.error(f"Failed to schedule hourly job: {e}", exc_info=True)
        raise

    # Promemoria email pre-scadenza: giornaliero, dopo il sync delle 08:30.
    try:
        reminder_trigger = CronTrigger(
            hour=config.REMINDER_EMAIL_HOUR,
            minute=config.REMINDER_EMAIL_MINUTE,
            timezone=config.TIMEZONE,
        )
        _scheduler.add_job(
            run_reminder_email_job,
            trigger=reminder_trigger,
            id="reminder_email_job",
            name="Promemoria email pre-scadenza",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=6 * 3600,  # un riavvio alle 09:15 non deve far saltare il giro
        )
        _scheduler.add_job(
            run_reminder_email_catchup,
            trigger="date",
            run_date=datetime.now(pytz.timezone(config.TIMEZONE)) + timedelta(seconds=180),
            id="reminder_email_catchup",
            name="Promemoria email: recupero dopo riavvio",
            replace_existing=True,
            max_instances=1,
        )
        logger.info(
            f"Scheduled reminder email job at {config.REMINDER_EMAIL_HOUR:02d}:"
            f"{config.REMINDER_EMAIL_MINUTE:02d} ({config.TIMEZONE})"
        )
    except Exception as e:
        logger.error(f"Failed to schedule reminder email job: {e}", exc_info=True)
        raise

    # Sync al boot (recupero da cold start di Render). LEGGERO (run_hourly_job,
    # senza l'aggancio ordini Shopify che è il passo più lento e CPU-pesante):
    # il full sync con order-matching lo fa il giornaliero alle 8:30. Un sync
    # pesante subito dopo il boot metteva sotto pressione l'istanza proprio
    # nella finestra in cui Render valuta l'health check. Ritardato a 120s per
    # dare tempo ai primi health check di passare.
    _scheduler.add_job(
        run_hourly_job,
        trigger="date",
        run_date=datetime.now(pytz.timezone(config.TIMEZONE)) + timedelta(seconds=120),
        id="startup_sync",
        name="Startup light sync after cold start",
        replace_existing=True,
        max_instances=1,
    )
    logger.info("Scheduled startup light sync in 120 seconds")

    _scheduler.start()
    _scheduler_started = True
    logger.info("Scheduler started")


def stop_scheduler():
    """Stop the background scheduler."""
    global _scheduler_started

    if not _scheduler_started or not _scheduler:
        logger.warning("Scheduler not running")
        return

    try:
        _scheduler.shutdown(wait=True)
        _scheduler_started = False
        logger.info("Scheduler stopped")
    except Exception as e:
        logger.error(f"Error stopping scheduler: {e}", exc_info=True)
        raise


def get_scheduler_status():
    """Get the current scheduler status.

    Campi aggiunti (senza rompere quelli esistenti):
    - hourly_enabled: il sync orario leggero è registrato
    - next_run_times: prossima esecuzione per job (ISO), quando disponibile
    """
    # Prossima esecuzione dei job (APScheduler la popola dopo lo start).
    next_run_times = {}
    if _scheduler is not None:
        for job_id in ("daily_sync_job", "hourly_sync_job", "startup_sync", "reminder_email_job"):
            try:
                job = _scheduler.get_job(job_id)
                if job is not None and job.next_run_time is not None:
                    next_run_times[job_id] = job.next_run_time.isoformat()
            except Exception:
                # get_job può fallire se lo scheduler non è avviato: non
                # deve mai far cadere lo status.
                pass

    return {
        "running": _scheduler_started,
        "scheduler_hour": config.SCHEDULER_HOUR,
        "scheduler_minute": config.SCHEDULER_MINUTE,
        "timezone": config.TIMEZONE,
        "hourly_enabled": True,
        "next_run_times": next_run_times,
        "last_sync": _last_sync,
    }
