import sys
import os
from sqlalchemy import text

# Add the app directory to the path so we can import models and database
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import engine

def migrate():
    with engine.connect() as connection:
        print("Checking if 'rank_order' exists in 'training_types'...")
        # Check if column exists (PostgreSQL)
        check_query = text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='training_types' AND column_name='rank_order';
        """)
        result = connection.execute(check_query).fetchone()
        
        if result:
            print("Column 'rank_order' already exists.")
        else:
            print("Adding column 'rank_order' to 'training_types'...")
            connection.execute(text("ALTER TABLE training_types ADD COLUMN rank_order INTEGER DEFAULT 0;"))
            connection.commit()
            print("Successfully added column 'rank_order'.")

if __name__ == "__main__":
    migrate()
