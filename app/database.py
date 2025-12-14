from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .config import settings

# HINWEIS: Für PostgreSQL/Supabase benötigen wir keine speziellen 'connect_args' 
# wie "ssl_disabled" mehr. Der Treiber handelt das automatisch.

engine = create_engine(
    settings.DATABASE_URL
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()