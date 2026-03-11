#!/bin/bash

# SC Recupero Crediti - Database Backup Script
# Backs up SQLite database and maintains 30-day retention
# Designed to be run via cron daily

set -e  # Exit on error

# Configuration variables
BACKUP_DIR="/opt/backups/sc-recupero"
PROJECT_DIR="/opt/sc-recupero-crediti"
DB_FILE="${PROJECT_DIR}/db.sqlite3"  # SQLite database location
RETENTION_DAYS=30
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/backup_${TIMESTAMP}.sqlite3"
LOG_FILE="${BACKUP_DIR}/backup.log"

# Ensure backup directory exists
mkdir -p "$BACKUP_DIR"

# Function to log messages
log_message() {
    local message="$1"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${timestamp}] ${message}" >> "$LOG_FILE"
    echo "[${timestamp}] ${message}"
}

# Function to cleanup old backups
cleanup_old_backups() {
    log_message "Cleaning up backups older than ${RETENTION_DAYS} days..."
    find "$BACKUP_DIR" -name "backup_*.sqlite3" -type f -mtime +${RETENTION_DAYS} -delete
    log_message "Cleanup completed"
}

# Function to send alert (optional)
send_alert() {
    local subject="$1"
    local message="$2"
    # Uncomment and configure if you have a mail system set up
    # echo "$message" | mail -s "$subject" admin@sakecompany.com
}

# Main backup process
log_message "========================================"
log_message "Starting database backup"
log_message "========================================"

# Check if database file exists
if [ ! -f "$DB_FILE" ]; then
    log_message "ERROR: Database file not found at $DB_FILE"
    send_alert "SC Recupero Crediti Backup Failed" "Database file not found at $DB_FILE"
    exit 1
fi

# Create backup
log_message "Backing up database from $DB_FILE..."
if cp "$DB_FILE" "$BACKUP_FILE"; then
    log_message "Backup created successfully: $BACKUP_FILE"

    # Get file size
    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log_message "Backup size: $BACKUP_SIZE"
else
    log_message "ERROR: Failed to create backup"
    send_alert "SC Recupero Crediti Backup Failed" "Failed to create backup at $BACKUP_FILE"
    exit 1
fi

# Verify backup integrity (if SQLite)
log_message "Verifying backup integrity..."
if sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" > /dev/null 2>&1; then
    log_message "Backup integrity verified: OK"
else
    log_message "WARNING: Backup integrity check may have failed"
    send_alert "SC Recupero Crediti Backup Warning" "Backup integrity check failed for $BACKUP_FILE"
fi

# Set proper permissions
chmod 600 "$BACKUP_FILE"
log_message "Set backup permissions to 600"

# Cleanup old backups
cleanup_old_backups

# Count total backups
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "backup_*.sqlite3" -type f | wc -l)
log_message "Total backups retained: $BACKUP_COUNT"

log_message "========================================"
log_message "Backup process completed successfully"
log_message "========================================"

# Optional: Create gzip compressed copy for long-term storage
log_message "Creating compressed backup..."
if gzip -c "$BACKUP_FILE" > "${BACKUP_FILE}.gz"; then
    log_message "Compressed backup created: ${BACKUP_FILE}.gz"
    # Keep compressed backups for 90 days
    find "$BACKUP_DIR" -name "backup_*.sqlite3.gz" -type f -mtime +90 -delete
fi

exit 0
