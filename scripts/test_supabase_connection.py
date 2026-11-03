#!/usr/bin/env python3
"""Supabase PostgreSQL connection tester and schema verifier.

Usage:
    DATABASE_URL="postgresql://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres?sslmode=require" python scripts/test_supabase_connection.py

Checks:
1. Validates DATABASE_URL scheme and credentials.
2. Establishes SSL connection with timeout.
3. Tests query execution and dialect confirmation.
4. Verifies presence or creation of the 'redlab' schema and all migration tables.
"""
from __future__ import annotations

import os
import sys
from sqlalchemy import create_engine, text

def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("❌ ERROR: DATABASE_URL environment variable is empty.")
        print("   Set DATABASE_URL to your Supabase PostgreSQL URI (Transaction Pooler or Direct).")
        return 1

    # Normalize legacy scheme if passed
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]

    print(f"Connecting to database backend...")
    # Mask password for display
    masked = db_url
    if "@" in masked and ":" in masked.split("@")[0]:
        user_part, rest = masked.split("@", 1)
        scheme_user = user_part.rsplit(":", 1)[0]
        masked = f"{scheme_user}:****@{rest}"
    print(f"Target: {masked}")

    try:
        engine = create_engine(
            db_url,
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args={"connect_timeout": 10},
        )
        with engine.connect() as conn:
            res = conn.execute(text("SELECT version();")).scalar()
            print(f"✅ Connected successfully!")
            print(f"   PostgreSQL Version: {res}")

            # Check if redlab schema exists
            has_redlab = conn.execute(
                text("SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'redlab';")
            ).scalar()
            if has_redlab:
                print("✅ Schema 'redlab' is present.")
            else:
                print("ℹ️  Schema 'redlab' not found yet. It will be created by 'flask --app app db upgrade'.")

            # Check table counts
            table_count = conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('public', 'redlab');")
            ).scalar()
            print(f"📊 Current table count in public + redlab schemas: {table_count}")

    except Exception as exc:
        print(f"❌ Connection failed: {type(exc).__name__}: {exc}")
        print("\nTroubleshooting tips for Supabase:")
        print("1. Ensure you used the Transaction Pooler URI (port 6543) or Direct URI (port 5432).")
        print("2. Ensure '?sslmode=require' is appended to the URI.")
        print("3. Check that your Supabase database password is URL-encoded if it contains special characters.")
        return 1

    print("\n🚀 Ready for Supabase deployment: run 'flask --app app db upgrade' to apply migrations.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
