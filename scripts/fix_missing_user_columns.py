
import os
import sys
from sqlalchemy import text, inspect
from app.database import engine, SessionLocal

def migrate():
    inspector = inspect(engine)
    columns = [c['name'] for c in inspector.get_columns('users')]
    
    print(f"Current columns in 'users': {columns}")
    
    db = SessionLocal()
    try:
        # Add notifications_email if missing
        if 'notifications_email' not in columns:
            print("Adding 'notifications_email' column...")
            db.execute(text("ALTER TABLE users ADD COLUMN notifications_email BOOLEAN DEFAULT TRUE"))
        
        # Add notifications_push if missing
        if 'notifications_push' not in columns:
            print("Adding 'notifications_push' column...")
            db.execute(text("ALTER TABLE users ADD COLUMN notifications_push BOOLEAN DEFAULT TRUE"))
            
        # Remove obsolete notification_settings if it exists
        if 'notification_settings' in columns:
            print("Removing obsolete 'notification_settings' column...")
            db.execute(text("ALTER TABLE users DROP COLUMN notification_settings"))
            
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
