import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Füge das Hauptverzeichnis zum Pfad hinzu
sys.path.append(os.getcwd())

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def sync():
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    
    print("Syncing 'is_billed' status with existing transactions...")
    try:
        # Finde alle Bookings, für die es eine passende Transaktion gibt.
        # Wir matchen über tenant_id, user_id und die Beschreibung der Transaktion (die den Termin-Titel enthält).
        # Da wir im SQL nicht einfach an den Titel des Termins kommen ohne Joins:
        # Wir joinen Bookings mit Appointments und suchen dann nach Transactions.
        
        query = text("""
            UPDATE bookings b
            SET is_billed = TRUE
            FROM appointments a, transactions t
            WHERE b.appointment_id = a.id
              AND b.user_id = t.user_id
              AND b.tenant_id = t.tenant_id
              AND t.description = 'Abrechnung: ' || a.title
              AND b.is_billed = FALSE;
        """)
        
        result = db.execute(query)
        db.commit()
        print(f"Sync successful: Updated {result.rowcount} bookings.")
    except Exception as e:
        print(f"Error during sync: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    sync()
