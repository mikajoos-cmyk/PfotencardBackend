from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
import os
from app.database import engine
from app.models import Tenant

def check_tenants():
    with Session(engine) as session:
        tenants = session.query(Tenant).all()
        print(f"Found {len(tenants)} tenants:")
        for t in tenants:
            print(f"- ID: {t.id}, Name: {t.name}, Subdomain: {t.subdomain}, Ends: {t.subscription_ends_at}, Status: {t.stripe_subscription_status}")

if __name__ == "__main__":
    try:
        check_tenants()
    except Exception as e:
        print(f"Error: {e}")
