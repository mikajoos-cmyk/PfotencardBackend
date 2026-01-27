import os
import sys
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Füge das Hauptverzeichnis zum Pfad hinzu
sys.path.append(os.getcwd())

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def migrate():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        print("Adding 'is_billed' column to 'bookings' table...")
        try:
            conn.execute(text("ALTER TABLE bookings ADD COLUMN is_billed BOOLEAN DEFAULT FALSE;"))
            conn.commit()
            print("Migration successful: Added 'is_billed' column.")
        except Exception as e:
            if "already exists" in str(e).lower():
                print("Column 'is_billed' already exists. Skipping.")
            else:
                print(f"Error during migration: {e}")

if __name__ == "__main__":
    migrate()
