
import os
import sys
from sqlalchemy import inspect
# Path trick to allow imports from app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.database import engine

def check_columns():
    inspector = inspect(engine)
    columns = inspector.get_columns('appointments')
    print("Columns in 'appointments' table:")
    for column in columns:
        print(f" - {column['name']} ({column['type']})")

if __name__ == "__main__":
    check_columns()
