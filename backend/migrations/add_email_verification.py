"""
Migration: Add email verification fields to users table.

Adds:
  - email_verified (Boolean, NOT NULL, default false)
  - verification_token (String(255), nullable)
  - verification_token_expires_at (DateTime, nullable)

On the run that adds email_verified only, sets email_verified=true and clears the
verification tokens for all existing rows (grandfather clause).
Idempotent: skips columns that already exist, and never repeats the grandfather clause.
Rolls back all changes on error.
"""

import logging

from sqlalchemy import inspect, text

from backend.db.database import engine

logger = logging.getLogger(__name__)


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    """Check if a column already exists on the given table."""
    columns = [col["name"] for col in inspector.get_columns(table_name)]
    return column_name in columns


def run_migration() -> None:
    """
    Execute the email verification migration.

    Adds email_verified, verification_token, and verification_token_expires_at
    columns to the users table if they don't already exist. Sets email_verified=true
    for all existing rows. Rolls back on any error.
    """
    inspector = inspect(engine)

    # Check if the users table exists at all
    if "users" not in inspector.get_table_names():
        logger.info("Migration skipped: 'users' table does not exist yet.")
        return

    # Get existing columns before starting the transaction
    existing_columns = {col["name"] for col in inspector.get_columns("users")}

    with engine.begin() as conn:
        try:
            # 1. Add email_verified column if it doesn't exist
            if "email_verified" not in existing_columns:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT false"
                    )
                )
                logger.info("Added column: email_verified")
            else:
                logger.info("Column email_verified already exists, skipping.")

            # 2. Add verification_token column if it doesn't exist
            if "verification_token" not in existing_columns:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN verification_token VARCHAR(255) DEFAULT NULL"
                    )
                )
                logger.info("Added column: verification_token")
            else:
                logger.info("Column verification_token already exists, skipping.")

            # 3. Add verification_token_expires_at column if it doesn't exist
            if "verification_token_expires_at" not in existing_columns:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN verification_token_expires_at TIMESTAMP DEFAULT NULL"
                    )
                )
                logger.info("Added column: verification_token_expires_at")
            else:
                logger.info("Column verification_token_expires_at already exists, skipping.")

            # 4-5. Grandfather clause, only on the run that adds email_verified. This migration
            # runs on every startup; repeating the clause would verify every user who signed up
            # since and clear every pending verification token.
            if "email_verified" not in existing_columns:
                conn.execute(text("UPDATE users SET email_verified = true"))
                logger.info("Set email_verified=true for all existing users.")
                conn.execute(
                    text(
                        "UPDATE users SET verification_token = NULL, "
                        "verification_token_expires_at = NULL"
                    )
                )
                logger.info(
                    "Set verification_token and verification_token_expires_at to NULL "
                    "for all existing users."
                )

        except Exception as e:
            logger.error(f"Email verification migration failed: {e}")
            raise  # Re-raise to trigger rollback via engine.begin() context manager

    logger.info("Email verification migration completed successfully.")
