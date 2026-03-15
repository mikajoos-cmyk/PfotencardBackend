
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
        # Update Certificate Templates
        columns = [c['name'] for c in inspector.get_columns('certificate_templates')]
        if 'body_text' not in columns:
            print("Adding 'body_text' column to 'certificate_templates' table...")
            db.execute(text("ALTER TABLE certificate_templates ADD COLUMN body_text TEXT"))
        
        if 'preview_data' not in columns:
            print("Adding 'preview_data' column to 'certificate_templates' table...")
            db.execute(text("ALTER TABLE certificate_templates ADD COLUMN preview_data JSONB"))
            
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
