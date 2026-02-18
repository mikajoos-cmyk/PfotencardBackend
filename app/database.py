from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .config import settings

# HINWEIS: Für PostgreSQL/Supabase benötigen wir keine speziellen 'connect_args' 
# wie "ssl_disabled" mehr. Der Treiber handelt das automatisch.

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()