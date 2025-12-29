from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models, crud
from app.models import Base
import os

# Connect to the REAL database used by the app
# Grab URL from .env or just use the local file if it's sqlite
# Since I can't easily read .env and parse it in one go without potential errors, 
# I'll try to rely on app.config logic or just assume standard dev setup if possible.
# But wait, looking at `database.py`:
# engine = create_engine(settings.DATABASE_URL)
# So I can just import engine/SessionLocal from app.database!

from app.database import SessionLocal

def debug_user(user_email_or_name):
    db = SessionLocal()
    try:
        # Try to find the user
        if str(user_email_or_name).isdigit():
             user = db.query(models.User).filter(models.User.id == int(user_email_or_name)).first()
        else:
            user = db.query(models.User).filter(
                (models.User.email == user_email_or_name) | (models.User.name.ilike(f"%{user_email_or_name}%"))
            ).first()

        if not user:
            print(f"User '{user_email_or_name}' not found.")
            return

        print(f"User: {user.name} (ID: {user.id})")
        print(f"Current Level ID: {user.current_level_id}")
        
        # Determine the logical 'previous' level if they just leveled up?
        # Or look at current level requirements if they failed to consume?
        # The user says "Prüfung consumed, training NOT consumed".
        # This implies they might still be at the OLD level if it failed? 
        # OR they are at the NEW level and training achievements are lingering.
        
        current_level = db.query(models.Level).filter(models.Level.id == user.current_level_id).first()
        print(f"Current Level: {current_level.name} (Rank: {current_level.rank_order})")

        # Check requirements for THIS level (maybe they are next level reqs?)
        # Let's check requirements for the PREVIOUS rank if possible
        prev_level = db.query(models.Level).filter(
            models.Level.tenant_id == user.tenant_id, 
            models.Level.rank_order == current_level.rank_order - 1
        ).first()
        
        if prev_level:
            print(f"--- Previous Level: {prev_level.name} (Rank: {prev_level.rank_order}) ---")
            reqs = db.query(models.LevelRequirement).filter(models.LevelRequirement.level_id == prev_level.id).all()
            for r in reqs:
                tt_name = r.training_type.name if r.training_type else "Unknown"
                tt_cat = r.training_type.category if r.training_type else "Unknown"
                print(f"  Req: Type={tt_name} (ID: {r.training_type_id}), Cat={tt_cat}, Count={r.required_count}, Additional={r.is_additional}")
                
                # Check unconsumed achievements for this type
                count = db.query(models.Achievement).filter(
                    models.Achievement.user_id == user.id,
                    models.Achievement.training_type_id == r.training_type_id,
                    models.Achievement.is_consumed == False
                ).count()
                print(f"    -> Unconsumed Achievements for this type: {count}")

        print(f"--- Current Level: {current_level.name} (Rank: {current_level.rank_order}) ---")
        reqs = db.query(models.LevelRequirement).filter(models.LevelRequirement.level_id == current_level.id).all()
        for r in reqs:
            tt_name = r.training_type.name if r.training_type else "Unknown"
            tt_cat = r.training_type.category if r.training_type else "Unknown"
            print(f"  Req: Type={tt_name} (ID: {r.training_type_id}), Cat={tt_cat}, Count={r.required_count}, Additional={r.is_additional}")
            
            achievements = db.query(models.Achievement).filter(
                models.Achievement.user_id == user.id,
                models.Achievement.training_type_id == r.training_type_id,
                models.Achievement.is_consumed == False
            ).all()
            print(f"    -> Unconsumed Achievements for this type: {len(achievements)}")
            for ach in achievements:
                print(f"       [ID: {ach.id}] Tenant: {ach.tenant_id}, Date: {ach.date_achieved}")
            
    finally:
        db.close()

if __name__ == "__main__":
    # Asking user for input might be tricky with run_command interaction
    # I'll just hardcode a name if I can guess it, otherwise I'll list users.
    debug_user("5")
