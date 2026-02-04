
from app.database import engine
from sqlalchemy import inspect

def check_schema():
    inspector = inspect(engine)
    
    # Check transactions table
    columns = [c['name'] for c in inspector.get_columns('transactions')]
    print(f"Transactions columns: {columns}")
    
    if 'invoice_number' not in columns:
        print("MISSING: invoice_number in transactions table")
    else:
        print("OK: invoice_number exists in transactions table")

    # Check system_sequences table
    tables = inspector.get_table_names()
    if 'system_sequences' not in tables:
        print("MISSING: system_sequences table")
    else:
        print("OK: system_sequences table exists")

if __name__ == "__main__":
    check_schema()
