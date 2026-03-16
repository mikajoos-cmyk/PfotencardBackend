from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta

from app import models, schemas, auth, database, crud, stripe_service
import stripe
from app.config import settings

router = APIRouter(
    prefix="/api/superadmin",
    tags=["superadmin"]
)

@router.post("/login", response_model=schemas.Token)
async def login_superadmin(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(database.get_db)
):
    # Super-Admin hat das is_superadmin Flag
    user = db.query(models.User).filter(
        models.User.email == form_data.username,
        models.User.is_superadmin == True
    ).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={"sub": user.email, "email": user.email, "role": "superadmin"}, 
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer", "user": user}

@router.get("/stats", response_model=schemas.SuperAdminStats, dependencies=[Depends(auth.get_current_superadmin)])
def get_stats(db: Session = Depends(database.get_db)):
    total_tenants = db.query(models.Tenant).count()
    active_tenants = db.query(models.Tenant).filter(models.Tenant.is_active == True).count()
    
    # Gesamtumsatz (Summe aller Transaktionen)
    total_revenue = db.query(models.Transaction).with_entities(models.func.sum(models.Transaction.amount)).scalar() or 0.0
    
    total_users = db.query(models.User).count()
    
    # Neue Tenants im letzten Monat
    last_month = datetime.now() - timedelta(days=30)
    new_tenants = db.query(models.Tenant).filter(models.Tenant.created_at >= last_month).count()
    
    return {
        "total_tenants": total_tenants,
        "active_tenants": active_tenants,
        "total_revenue": total_revenue,
        "total_users": total_users,
        "new_tenants_last_month": new_tenants
    }

@router.get("/tenants", response_model=List[schemas.Tenant], dependencies=[Depends(auth.get_current_superadmin)])
def get_tenants(db: Session = Depends(database.get_db)):
    return db.query(models.Tenant).all()

@router.get("/users", response_model=List[schemas.User], dependencies=[Depends(auth.get_current_superadmin)])
def get_users(tenant_id: Optional[int] = None, db: Session = Depends(database.get_db)):
    query = db.query(models.User)
    if tenant_id:
        query = query.filter(models.User.tenant_id == tenant_id)
    return query.all()

@router.put("/users/{user_id}/ban", dependencies=[Depends(auth.get_current_superadmin)])
def ban_user(user_id: int, active: bool = False, db: Session = Depends(database.get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = active
    db.commit()
    return {"message": f"User {'banned' if not active else 'unbanned'} successfully"}

@router.put("/tenants/{tenant_id}/plan", dependencies=[Depends(auth.get_current_superadmin)])
def update_tenant_plan(tenant_id: int, plan: str, db: Session = Depends(database.get_db)):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenant.plan = plan
    db.commit()
    return {"message": f"Tenant plan updated to {plan}"}

@router.get("/packages", response_model=List[schemas.SubscriptionPackage], dependencies=[Depends(auth.get_current_superadmin)])
def get_packages(db: Session = Depends(database.get_db)):
    return db.query(models.SubscriptionPackage).order_by(models.SubscriptionPackage.price_monthly.asc()).all()

@router.post("/packages", response_model=schemas.SubscriptionPackage, dependencies=[Depends(auth.get_current_superadmin)])
def create_package(package: schemas.SubscriptionPackageCreate, db: Session = Depends(database.get_db)):
    # 1. Produkt in Stripe anlegen (falls noch nicht vorhanden)
    try:
        product = stripe.Product.create(
            name=f"PfotenCard {package.plan_name.capitalize()}",
            description=f"{'Zusatzmodul' if package.package_type == 'addon' else 'Basis-Paket'}"
        )
        
        # 2a. Monatlicher Fixpreis (Grundgebühr)
        price_base_monthly = stripe.Price.create(
            product=product.id,
            unit_amount=int(package.price_monthly * 100), # Stripe rechnet in Cent!
            currency="eur",
            recurring={"interval": "month"}
        )

        # 2b. Jährlicher Fixpreis (Grundgebühr)
        price_base_yearly = stripe.Price.create(
            product=product.id,
            unit_amount=int(package.price_yearly * 100),
            currency="eur",
            recurring={"interval": "year"}
        )
        
        # 3. Nutzungsbasierter Preis (Zusätzliche Kunden)
        # Wenn kein Preis angegeben ist, nehmen wir 0
        price_users = stripe.Price.create(
            product=product.id,
            unit_amount=int(package.additional_cost_per_customer * 100),
            currency="eur",
            recurring={
                "interval": "month", 
                "usage_type": "metered",
                "meter": settings.STRIPE_METER_ID_USERS
            },
        )
        
        # 4. Nutzungsbasierter Preis (Transaktionsgebühren) -> Immer 1 Cent!
        # Dieser Preis wird für variable Gebühren genutzt, indem man die Cent-Anzahl als Usage meldet.
        price_fees = stripe.Price.create(
            product=product.id,
            unit_amount=1, 
            currency="eur",
            recurring={
                "interval": "month", 
                "usage_type": "metered",
                "meter": settings.STRIPE_METER_ID_FEES
            },
        )
        
        # 5. Alles in der Datenbank speichern
        db_package = models.SubscriptionPackage(
            **package.dict(exclude={"stripe_product_id", "stripe_price_id_base_monthly", "stripe_price_id_base_yearly", "stripe_price_id_users", "stripe_price_id_fees"}),
            stripe_product_id=product.id,
            stripe_price_id_base_monthly=price_base_monthly.id,
            stripe_price_id_base_yearly=price_base_yearly.id,
            stripe_price_id_users=price_users.id,
            stripe_price_id_fees=price_fees.id
        )
        db.add(db_package)
        db.commit()
        db.refresh(db_package)
        return db_package
        
    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Stripe Error: {str(e)}")

@router.put("/packages/{package_id}", response_model=schemas.SubscriptionPackage, dependencies=[Depends(auth.get_current_superadmin)])
def update_package(package_id: int, package: schemas.SubscriptionPackageCreate, db: Session = Depends(database.get_db)):
    db_package = db.query(models.SubscriptionPackage).filter(models.SubscriptionPackage.id == package_id).first()
    if not db_package:
        raise HTTPException(status_code=404, detail="Package not found")
    
    try:
        # Prüfen ob monatlicher Preis geändert wurde -> Neuen Stripe Price anlegen
        if db_package.price_monthly != package.price_monthly:
            if db_package.stripe_price_id_base_monthly:
                stripe.Price.modify(db_package.stripe_price_id_base_monthly, active=False)
            
            new_price_base_monthly = stripe.Price.create(
                product=db_package.stripe_product_id,
                unit_amount=int(package.price_monthly * 100),
                currency="eur",
                recurring={"interval": "month"}
            )
            db_package.stripe_price_id_base_monthly = new_price_base_monthly.id

        # Prüfen ob jährlicher Preis geändert wurde
        if db_package.price_yearly != package.price_yearly:
            if db_package.stripe_price_id_base_yearly:
                stripe.Price.modify(db_package.stripe_price_id_base_yearly, active=False)
            
            new_price_base_yearly = stripe.Price.create(
                product=db_package.stripe_product_id,
                unit_amount=int(package.price_yearly * 100),
                currency="eur",
                recurring={"interval": "year"}
            )
            db_package.stripe_price_id_base_yearly = new_price_base_yearly.id

        # Prüfen ob zusätzliche Kosten pro Kunde geändert wurden
        if db_package.additional_cost_per_customer != package.additional_cost_per_customer:
            if db_package.stripe_price_id_users:
                stripe.Price.modify(db_package.stripe_price_id_users, active=False)
            
            new_price_users = stripe.Price.create(
                product=db_package.stripe_product_id,
                unit_amount=int(package.additional_cost_per_customer * 100),
                currency="eur",
                recurring={
                    "interval": "month", 
                    "usage_type": "metered",
                    "meter": settings.STRIPE_METER_ID_USERS
                },
            )
            db_package.stripe_price_id_users = new_price_users.id

        # Restliche Felder updaten
        update_data = package.dict(exclude_unset=True)
        for key, value in update_data.items():
            if not key.startswith("stripe_"): # Stripe IDs werden manuell/automatisch verwaltet
                setattr(db_package, key, value)
        
        db.commit()
        db.refresh(db_package)
        return db_package

    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Stripe Error: {str(e)}")
