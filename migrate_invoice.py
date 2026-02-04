
from sqlalchemy import text
from app.database import engine

def migrate():
    with engine.connect() as conn:
        print("Adding invoice_number to transactions...")
        try:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN invoice_number VARCHAR(50) UNIQUE"))
            conn.commit()
            print("Successfully added invoice_number column.")
        except Exception as e:
            print(f"Error adding column: {e}")
            
if __name__ == "__main__":
    migrate()
