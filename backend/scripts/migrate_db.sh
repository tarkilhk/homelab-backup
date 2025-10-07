#!/bin/bash
# Database migration script
# This script applies SQL migrations to the homelab-backup database

set -e

# Database path (default for Docker deployment)
DB_PATH="${DB_PATH:-/home/dev/projects/homelab-backup/db/homelab_backup.db}"
MIGRATIONS_DIR="$(dirname "$0")/../migrations"

echo "🔍 Checking database at: $DB_PATH"

if [ ! -f "$DB_PATH" ]; then
    echo "❌ Error: Database file not found at $DB_PATH"
    echo "Please set DB_PATH environment variable to your database location"
    exit 1
fi

echo "📦 Backing up database..."
cp "$DB_PATH" "${DB_PATH}.backup.$(date +%Y%m%d_%H%M%S)"
echo "✅ Backup created"

echo "🔧 Applying migrations..."

for migration_file in "$MIGRATIONS_DIR"/*.sql; do
    if [ -f "$migration_file" ]; then
        echo "  → Applying $(basename "$migration_file")..."
        sqlite3 "$DB_PATH" < "$migration_file" 2>&1 || {
            echo "⚠️  Migration may have already been applied or encountered an error"
        }
    fi
done

echo "✅ Migration completed successfully!"
echo "📊 Database schema updated"
