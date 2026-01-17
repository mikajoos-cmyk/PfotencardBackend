
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
        # 1. Update Levels
        level_columns = [c['name'] for c in inspector.get_columns('levels')]
        if 'color' not in level_columns:
            print("Adding 'color' column to 'levels' table...")
            db.execute(text("ALTER TABLE levels ADD COLUMN color VARCHAR(50)"))
        
        # 2. Update Appointments
        appt_columns = [c['name'] for c in inspector.get_columns('appointments')]
        if 'is_open_for_all' not in appt_columns:
            print("Adding 'is_open_for_all' column to 'appointments' table...")
            db.execute(text("ALTER TABLE appointments ADD COLUMN is_open_for_all BOOLEAN DEFAULT FALSE"))
            
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
