
import os
import sys
from sqlalchemy import text, inspect, Integer, Boolean
from app.database import engine, SessionLocal

def migrate():
    inspector = inspect(engine)
    columns = [c['name'] for c in inspector.get_columns('users')]
    
    print(f"Current columns in 'users': {columns}")
    
    db = SessionLocal()
    try:
        # 1. Basic Notification Toggles
        if 'notifications_email' not in columns:
            print("Adding 'notifications_email' column...")
            db.execute(text("ALTER TABLE users ADD COLUMN notifications_email BOOLEAN DEFAULT TRUE"))
        
        if 'notifications_push' not in columns:
            print("Adding 'notifications_push' column...")
            db.execute(text("ALTER TABLE users ADD COLUMN notifications_push BOOLEAN DEFAULT TRUE"))

        # 2. Granular E-Mail Settings
        email_cols = [
            'notif_email_overall',
            'notif_email_chat',
            'notif_email_news',
            'notif_email_booking',
            'notif_email_reminder',
            'notif_email_alert'
        ]
        for col in email_cols:
            if col not in columns:
                print(f"Adding '{col}' column...")
                db.execute(text(f"ALTER TABLE users ADD COLUMN {col} BOOLEAN DEFAULT TRUE"))

        # 3. Granular Push Settings
        push_cols = [
            'notif_push_overall',
            'notif_push_chat',
            'notif_push_news',
            'notif_push_booking',
            'notif_push_reminder',
            'notif_push_alert'
        ]
        for col in push_cols:
            if col not in columns:
                print(f"Adding '{col}' column...")
                db.execute(text(f"ALTER TABLE users ADD COLUMN {col} BOOLEAN DEFAULT TRUE"))

        # 4. Reminder Offset
        if 'reminder_offset_minutes' not in columns:
            print("Adding 'reminder_offset_minutes' column...")
            db.execute(text("ALTER TABLE users ADD COLUMN reminder_offset_minutes INTEGER DEFAULT 60"))

        # 5. Cleanup
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
