
from sqlalchemy import inspect
from app.database import engine
from app.models import Base

def check_schema():
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"--- TABLES IN DATABASE ---")
    print(", ".join(tables))
    
    for table_name in sorted(tables):
        columns = inspector.get_columns(table_name)
        print(f"\n[Table: {table_name}]")
        for column in columns:
            print(f"  - {column['name']} ({column['type']})")

if __name__ == "__main__":
    check_schema()
