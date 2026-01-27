
import os
import sys
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Env laden
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("DATABASE_URL not found in .env")
    sys.exit(1)

# Supabase fix for SQLAlchemy (postgres:// -> postgresql://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

def run_migration():
    migrations = [
        # 1. Dogs Table: Add current_level_id
        "ALTER TABLE dogs ADD COLUMN IF NOT EXISTS current_level_id INTEGER REFERENCES levels(id) ON DELETE SET NULL;",
        
        # 2. Achievements Table: Add dog_id
        "ALTER TABLE achievements ADD COLUMN IF NOT EXISTS dog_id INTEGER REFERENCES dogs(id) ON DELETE CASCADE;",
        
        # 3. Bookings Table: Add dog_id
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dog_id INTEGER REFERENCES dogs(id) ON DELETE CASCADE;",
        
        # 4. Update Constraints for Bookings
        # Drop old constraint
        "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS uix_appointment_user;",
        # Add new constraint (including dog_id)
        "ALTER TABLE bookings ADD CONSTRAINT uix_appointment_user_dog UNIQUE (appointment_id, user_id, dog_id);"
    ]

    with engine.connect() as conn:
        print("Starting Database Migration...")
        for query in migrations:
            try:
                print(f"Executing: {query}")
                conn.execute(text(query))
                conn.commit()
                print("Success")
            except Exception as e:
                print(f"Error executing query: {e}")
        print("Migration Finished.")

if __name__ == "__main__":
    run_migration()
