
import os
import sys
from sqlalchemy import text, create_engine
from dotenv import load_dotenv

# Add parent dir to path to import app config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Try to construct from components if direct URL is missing
    DB_USER = os.getenv("DB_USER", "postgres")
    DB_PASS = os.getenv("DB_PASS", "postgres")
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = os.getenv("DB_PORT", "5432")
    DB_NAME = os.getenv("DB_NAME", "pfotencard")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

print(f"Connecting to {DATABASE_URL}...")
engine = create_engine(DATABASE_URL)

sql = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS permissions JSONB DEFAULT '{
    "can_create_courses": false,
    "can_edit_status": false,
    "can_delete_customers": false,
    "can_create_messages": false
}';
"""

with engine.connect() as conn:
    conn.execute(text(sql))
    conn.commit()
    print("Column 'permissions' added successfully to 'users' table.")
