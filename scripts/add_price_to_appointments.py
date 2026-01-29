
import os
import sys
from sqlalchemy import text, inspect
# Path trick to allow imports from app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.database import engine, SessionLocal

def migrate():
    inspector = inspect(engine)
    
    db = SessionLocal()
    try:
        # Update Appointments
        appt_columns = [c['name'] for c in inspector.get_columns('appointments')]
        if 'price' not in appt_columns:
            print("Adding 'price' column to 'appointments' table...")
            db.execute(text("ALTER TABLE appointments ADD COLUMN price FLOAT"))
            
        db.commit()
        print("Migration completed successfully.")
    except Exception as e:
        db.rollback()
        print(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    migrate()
