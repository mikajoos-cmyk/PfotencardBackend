from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
from uuid import UUID
import uuid
import httpx

from app import models, schemas, auth, database, crud, stripe_service
import stripe
from app.config import settings

router = APIRouter(
    prefix="/api/superadmin",
    tags=["superadmin"]
)

@router.get("/validate-vat/{vat_id}")
async def validate_vat(vat_id: str):
    """
    Validiert eine europäische USt-IdNr. über eine öffentliche VIES API.
    Ersetzt die Supabase Edge Function aus CreatorStay.
    """
    vat_id = vat_id.replace(" ", "").upper()
    if len(vat_id) < 4:
        raise HTTPException(status_code=400, detail="Ungültiges Format")
        
    country_code = vat_id[:2]
    vat_number = vat_id[2:]
    
    try:
        # Nutzung einer kostenlosen VIES REST API (oder der offiziellen SOAP API)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"https://vat.erply.com/v1/check?vat_number={vat_number}&country_code={country_code}")
            
            if response.status_code == 200:
                data = response.json()
                is_valid = data.get("valid", False)
                return {
                    "valid": is_valid,
                    "company_name": data.get("name", ""),
                    "address": data.get("address", "")
                }
            else:
                # Fallback, falls die API down ist
                return {"valid": False, "error": "Prüfung derzeit nicht möglich"}
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

    # Promo Codes Gesamtzahl
    total_promo_codes = db.query(models.PromoCode).count()
    
    return {
        "total_tenants": total_tenants,
        "active_tenants": active_tenants,
        "total_revenue": total_revenue,
        "total_users": total_users,
        "new_tenants_last_month": new_tenants,
        "total_promo_codes": total_promo_codes
    }

@router.get("/tenants", response_model=List[schemas.Tenant], dependencies=[Depends(auth.get_current_superadmin)])
def get_tenants(db: Session = Depends(database.get_db)):
    return db.query(models.Tenant).all()

@router.get("/billing/payment-methods")
def get_payment_methods(request: Request, db: Session = Depends(database.get_db)):
    """
    Liefert die gespeicherten Zahlungsmethoden (Kreditkarten) für den Tenant,
    der über den Header "x-tenant-subdomain" identifiziert wird.
    """
    subdomain = request.headers.get("x-tenant-subdomain")
    if not subdomain:
        raise HTTPException(status_code=400, detail="Subdomain missing")

    tenant = db.query(models.Tenant).filter(models.Tenant.subdomain == subdomain).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return stripe_service.get_saved_payment_methods(db, tenant.id)

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
            recurring={"interval": "month"},
            tax_behavior="exclusive"
        )

        # 2b. Jährlicher Fixpreis (Grundgebühr)
        price_base_yearly = stripe.Price.create(
            product=product.id,
            unit_amount=int(package.price_yearly * 100),
            currency="eur",
            recurring={"interval": "year"},
            tax_behavior="exclusive"
        )
        
        # 3. Nutzungsbasierte Preise NUR für Basis-Pakete erstellen
        price_users_id = None
        price_fees_id = None
        
        if package.package_type == 'base':
            # NEU: Hole die Meter-IDs dynamisch aus Stripe!
            meter_users_id, meter_fees_id = stripe_service.get_or_create_meters()
            
            price_users = stripe.Price.create(
                product=product.id,
                unit_amount=int(package.additional_cost_per_customer * 100),
                currency="eur",
                recurring={
                    "interval": "month", 
                    "usage_type": "metered",
                    "meter": meter_users_id  # <--- HIER: Dynamische ID statt settings!
                },
                tax_behavior="exclusive"
            )
            price_users_id = price_users.id
            
            price_fees = stripe.Price.create(
                product=product.id,
                unit_amount=1, 
                currency="eur",
                recurring={
                    "interval": "month", 
                    "usage_type": "metered",
                    "meter": meter_fees_id   # <--- HIER: Dynamische ID statt settings!
                },
                tax_behavior="exclusive"
            )
            price_fees_id = price_fees.id
        
        # 5. Alles in der Datenbank speichern
        db_package = models.SubscriptionPackage(
            **package.dict(exclude={"stripe_product_id", "stripe_price_id_base_monthly", "stripe_price_id_base_yearly", "stripe_price_id_users", "stripe_price_id_fees"}),
            stripe_product_id=product.id,
            stripe_price_id_base_monthly=price_base_monthly.id,
            stripe_price_id_base_yearly=price_base_yearly.id,
            stripe_price_id_users=price_users_id,
            stripe_price_id_fees=price_fees_id
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
                recurring={"interval": "month"},
                tax_behavior="exclusive"
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
                recurring={"interval": "year"},
                tax_behavior="exclusive"
            )
            db_package.stripe_price_id_base_yearly = new_price_base_yearly.id

        # Prüfen ob zusätzliche Kosten pro Kunde geändert wurden (Nur für Basis-Pakete)
        if package.package_type == 'base' and db_package.additional_cost_per_customer != package.additional_cost_per_customer:
            if db_package.stripe_price_id_users:
                stripe.Price.modify(db_package.stripe_price_id_users, active=False)
            
            # NEU: Hole die Meter-IDs dynamisch aus Stripe!
            meter_users_id, _ = stripe_service.get_or_create_meters()
            
            new_price_users = stripe.Price.create(
                product=db_package.stripe_product_id,
                unit_amount=int(package.additional_cost_per_customer * 100),
                currency="eur",
                recurring={
                    "interval": "month", 
                    "usage_type": "metered",
                    "meter": meter_users_id
                },
                tax_behavior="exclusive"
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

# --- PROMO CODES ---

@router.get("/promo-codes", response_model=List[schemas.PromoCode], dependencies=[Depends(auth.get_current_superadmin)])
def get_promo_codes(db: Session = Depends(database.get_db)):
    return db.query(models.PromoCode).order_by(models.PromoCode.created_at.desc()).all()

@router.post("/promo-codes", response_model=schemas.PromoCode)
def create_promo_code(
    promo: schemas.PromoCodeCreate, 
    current_user: models.User = Depends(auth.get_current_superadmin), 
    db: Session = Depends(database.get_db)
):
    # 1. Stripe Product IDs für applicable_plans holen
    stripe_product_ids = []
    if promo.applicable_plans:
        packages = db.query(models.SubscriptionPackage).filter(
            models.SubscriptionPackage.plan_name.in_(promo.applicable_plans),
            models.SubscriptionPackage.stripe_product_id.isnot(None)
        ).all()
        stripe_product_ids = [p.stripe_product_id for p in packages]

    try:
        # 2. Stripe Coupon erstellen
        stripe.api_key = settings.STRIPE_SECRET_KEY
        coupon = stripe.Coupon.create(
            percent_off=100,
            duration='repeating',
            duration_in_months=promo.duration_months,
            applies_to={'products': stripe_product_ids} if stripe_product_ids else None,
            name=promo.name or promo.code,
        )

        # 3. Stripe Promotion Code erstellen
        promotion_code = stripe.PromotionCode.create(
            coupon=coupon.id,
            code=promo.code.upper(),
            max_redemptions=promo.max_uses,
            expires_at=int(promo.expires_at.timestamp()) if promo.expires_at else None,
        )

        # 4. In Datenbank speichern
        db_promo = models.PromoCode(
            **promo.dict(),
            code=promo.code.upper(),
            stripe_coupon_id=coupon.id,
            stripe_promotion_code_id=promotion_code.id,
            created_by=None # In FastAPI haben wir keine Supabase User ID direkt, wir könnten current_user.id nehmen
        )
        # Wenn current_user.auth_id vorhanden ist (UUID), nutzen wir diese
        if hasattr(current_user, 'auth_id') and current_user.auth_id:
            db_promo.created_by = current_user.auth_id

        db.add(db_promo)
        db.commit()
        db.refresh(db_promo)
        return db_promo

    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Stripe Error: {str(e)}")

@router.put("/promo-codes/{promo_id}", response_model=schemas.PromoCode, dependencies=[Depends(auth.get_current_superadmin)])
def update_promo_code(promo_id: UUID, promo_update: schemas.PromoCodeUpdate, db: Session = Depends(database.get_db)):
    db_promo = db.query(models.PromoCode).filter(models.PromoCode.id == promo_id).first()
    if not db_promo:
        raise HTTPException(status_code=404, detail="Promo code not found")

    try:
        # Falls is_active geändert wurde -> Stripe Sync
        if promo_update.is_active is not None and promo_update.is_active != db_promo.is_active:
            stripe.api_key = settings.STRIPE_SECRET_KEY
            if db_promo.stripe_promotion_code_id:
                stripe.PromotionCode.modify(
                    db_promo.stripe_promotion_code_id,
                    active=promo_update.is_active
                )
        
        # Felder updaten
        update_data = promo_update.dict(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_promo, key, value)
        
        db.commit()
        db.refresh(db_promo)
        return db_promo

    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Stripe Error: {str(e)}")

@router.delete("/promo-codes/{promo_id}", dependencies=[Depends(auth.get_current_superadmin)])
def delete_promo_code(promo_id: UUID, db: Session = Depends(database.get_db)):
    db_promo = db.query(models.PromoCode).filter(models.PromoCode.id == promo_id).first()
    if not db_promo:
        raise HTTPException(status_code=404, detail="Promo code not found")

    try:
        # Stripe Promotion Code deaktivieren
        stripe.api_key = settings.STRIPE_SECRET_KEY
        if db_promo.stripe_promotion_code_id:
            try:
                stripe.PromotionCode.modify(db_promo.stripe_promotion_code_id, active=False)
            except:
                pass
        
        db.delete(db_promo)
        db.commit()
        return {"message": "Promo code deleted successfully"}

    except stripe.error.StripeError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Stripe Error: {str(e)}")

@router.get("/promo-redemptions", response_model=List[schemas.PromoCodeRedemption], dependencies=[Depends(auth.get_current_superadmin)])
def get_promo_redemptions(db: Session = Depends(database.get_db)):
    return db.query(models.PromoCodeRedemption).order_by(models.PromoCodeRedemption.created_at.desc()).all()
