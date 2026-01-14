# app/main.py
import os
import shutil
from starlette.responses import FileResponse
from fastapi import Depends, FastAPI, HTTPException, status, UploadFile, File, Request, Header
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import secrets
import stripe

from . import crud, models, schemas, auth, stripe_service, legal
from .storage_service import delete_file_from_storage, delete_folder_from_storage
from .database import engine, get_db, SessionLocal
from .config import settings
from supabase import create_client, Client

models.Base.metadata.create_all(bind=engine)
app = FastAPI()

supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

origins_regex = r"https://(.*\.)?pfotencard\.de|http://localhost:\d+"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=origins_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(legal.router, prefix="/api/legal", tags=["legal"])

@app.get("/")
def read_root():
    return {"message": "Pfotencard Multi-Tenant API is running"}

@app.get("/api/config", response_model=schemas.AppConfig)
def read_app_config(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    return crud.get_app_config(db, tenant.id)

@app.get("/api/status", response_model=schemas.AppStatus)
def read_app_status(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    return crud.get_app_status(db, tenant.id)

@app.put("/api/status", response_model=schemas.AppStatus)
def update_app_status(
    status_update: schemas.AppStatusUpdate,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.update_app_status(db, tenant.id, status_update)

@app.put("/api/settings")
def update_settings(
    settings: schemas.SettingsUpdate,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    crud.update_tenant_settings(db, tenant.id, settings)
    return {"message": "Settings updated successfully"}

@app.post("/api/login", response_model=schemas.Token)
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    user = crud.get_user_by_email(db, email=form_data.username, tenant_id=tenant.id)
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={"sub": user.email, "tenant_id": tenant.id}, 
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer", "user": user}

@app.get("/api/users/me", response_model=schemas.User)
async def read_users_me(current_user: schemas.User = Depends(auth.get_current_active_user)):
    return current_user

# --- TENANT STATUS & SUBSCRIPTION ---

@app.get("/api/tenants/status", response_model=schemas.TenantStatus)
def check_tenant_status(subdomain: str, db: Session = Depends(get_db)):
    tenant = crud.get_tenant_by_subdomain(db, subdomain)
    if not tenant:
        return {"exists": False}
    
    now = datetime.now(timezone.utc)
    is_valid = True
    
    if tenant.subscription_ends_at and tenant.subscription_ends_at < now:
        is_valid = False

    has_stripe = True if tenant.stripe_subscription_id else False
    
    # Trial Logik verbessert: Auch Stripe Trial status berücksichtigen
    in_trial = False
    # Wenn "Registrierungs-Trial" ohne Stripe 
    if is_valid and not has_stripe:
        in_trial = True
    # Oder wenn Stripe-Status 'trialing' ist
    elif tenant.stripe_subscription_status == 'trialing':
        in_trial = True

    return {
        "exists": True, 
        "name": tenant.name,
        "subscription_valid": is_valid,
        "subscription_ends_at": tenant.subscription_ends_at,
        "plan": tenant.plan,
        "has_payment_method": has_stripe,
        "in_trial": in_trial,
        
        # NEU: Die DB-Werte zurückgeben
        "stripe_subscription_status": tenant.stripe_subscription_status,
        "cancel_at_period_end": tenant.cancel_at_period_end,
        
        # NEU: Vorschau-Daten
        "next_payment_amount": tenant.next_payment_amount,
        "next_payment_date": tenant.next_payment_date,
        "upcoming_plan": tenant.upcoming_plan,
        
        # NEU: AVV Status
        "avv_accepted_at": tenant.avv_accepted_at,
        "avv_version": tenant.avv_accepted_version
    }

# Sicherheit: Nur mit Secret Key ausführbar
from .config import settings
CRON_SECRET = settings.CRON_SECRET
if not CRON_SECRET:
    # Warnung loggen oder Exception werfen, um das Deployment zu stoppen
    raise RuntimeError("CRON_SECRET env var is missing")

@app.delete("/api/cron/cleanup-abandoned-tenants")
def cleanup_abandoned_tenants(x_cron_secret: str = Header(None), db: Session = Depends(get_db)):
    """
    Löscht Tenants, die vor >30 Tagen erstellt wurden, aber KEIN aktives Abo haben (trial_end vorbei).
    Dies erfüllt den Grundsatz der Datensparsamkeit.
    """
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 1. Finde verwaiste Tenants (Beispiel-Logik)
    # Definiere "verwaist": Erstellt vor 30 Tagen UND kein Stripe Customer ID (nie Checkout gestartet)
    # ODER status='cancelled' und cancellation_date > 30 Tage her.
    
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    
    abandoned_tenants = db.query(models.Tenant).filter(
        models.Tenant.created_at < thirty_days_ago,
        models.Tenant.stripe_customer_id == None  # Nie bezahlt
    ).all()
    
    deleted_count = 0
    
    for tenant in abandoned_tenants:
        # A. Storage bereinigen
        # Lösche alles im Bucket-Ordner des Tenants (falls du Ordner pro Tenant hast)
        delete_folder_from_storage(supabase, "documents", f"{tenant.id}")
        
        # B. DB Eintrag löschen (Cascading löscht User, Dogs etc.)
        db.delete(tenant)
        deleted_count += 1
        
    db.commit()
    # logger.info(f"Cron Cleanup: Deleted {deleted_count} abandoned tenants.") # logger not initialized in main.py, using print
    print(f"Cron Cleanup: Deleted {deleted_count} abandoned tenants.")
    
    return {"status": "ok", "deleted": deleted_count}



@app.post("/api/tenants/subscribe")
def update_subscription(data: schemas.SubscriptionUpdate, db: Session = Depends(get_db)):
    tenant = crud.get_tenant_by_subdomain(db, data.subdomain)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    
    tenant.plan = data.plan
    tenant.subscription_ends_at = datetime.now(timezone.utc) + timedelta(days=365)
    tenant.is_active = True
    
    db.commit()
    return {"message": "Subscription updated successfully", "valid_until": tenant.subscription_ends_at}


# --- STRIPE WEBHOOK (AKTUALISIERT) ---

# Hilfsfunktion, um Plan-Namen aus Preis-ID zu ermitteln
def get_plan_name_from_price_id(price_id: str):
    """Maps Stripe price ID to plan name"""
    s = settings
    if price_id in [s.STRIPE_PRICE_ID_STARTER_MONTHLY, s.STRIPE_PRICE_ID_STARTER_YEARLY]: 
        return "starter"
    if price_id in [s.STRIPE_PRICE_ID_PRO_MONTHLY, s.STRIPE_PRICE_ID_PRO_YEARLY]: 
        return "pro"
    if price_id in [s.STRIPE_PRICE_ID_ENTERPRISE_MONTHLY, s.STRIPE_PRICE_ID_ENTERPRISE_YEARLY]: 
        return "enterprise"
    return None

# --- HILFSFUNKTION FÜR ROBUSTE ID-EXTRAKTION ---
def get_subscription_id_safe(invoice: dict) -> Optional[str]:
    """
    Versucht, die Subscription-ID aus verschiedenen Ebenen des Invoice-Objekts zu extrahieren.
    Funktioniert für alte und neue Stripe API-Versionen (z.B. 2025-11-17.clover).
    """
    print("DEBUG: Starte Subscription ID Extraktion...")
    
    # 1. Versuch: Standard-Feld auf oberster Ebene (ältere APIs)
    if invoice.get('subscription'):
        print(f"DEBUG: Subscription ID gefunden (Standard-Feld): {invoice.get('subscription')}")
        return invoice.get('subscription')

    # 2. Versuch: Verschachtelt in 'parent' (neue APIs)
    # Pfad: parent -> subscription_details -> subscription
    parent = invoice.get('parent')
    if parent and isinstance(parent, dict):
        print("DEBUG: Parent-Objekt gefunden, prüfe subscription_details...")
        sub_details = parent.get('subscription_details')
        if sub_details and isinstance(sub_details, dict):
            if sub_details.get('subscription'):
                print(f"DEBUG: Subscription ID gefunden (Parent->subscription_details): {sub_details.get('subscription')}")
                return sub_details.get('subscription')

    # 3. Versuch: Über die Rechnungspositionen (Line Items)
    # Manchmal fehlt die Info im Header, steht aber bei den Posten dabei
    lines = invoice.get('lines', {})
    if lines and isinstance(lines, dict) and 'data' in lines:
        print(f"DEBUG: Prüfe {len(lines['data'])} Line Items...")
        for idx, item in enumerate(lines['data']):
            # 3a. Direkt im Line Item
            if item.get('subscription'):
                print(f"DEBUG: Subscription ID gefunden (Line Item {idx}): {item.get('subscription')}")
                return item.get('subscription')
            
            # 3b. Verschachtelt im Line Item Parent
            # Pfad: item -> parent -> subscription_item_details -> subscription
            item_parent = item.get('parent')
            if item_parent and isinstance(item_parent, dict):
                item_sub_details = item_parent.get('subscription_item_details')
                if item_sub_details and isinstance(item_sub_details, dict):
                    if item_sub_details.get('subscription'):
                        print(f"DEBUG: Subscription ID gefunden (Line Item {idx} Parent): {item_sub_details.get('subscription')}")
                        return item_sub_details.get('subscription')
    
    print("DEBUG: Keine Subscription ID gefunden in allen Versuchen")
    return None

@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    sig_header = stripe_signature
    endpoint_secret = settings.STRIPE_WEBHOOK_SECRET

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # --- EVENT HANDLING ---
    if event['type'] == 'customer.subscription.updated':
        subscription = event['data']['object']
        await handle_subscription_update(subscription)

    elif event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        await handle_subscription_deleted(subscription)

    # NEU: Handler für erfolgreiche Zahlungen (Verlängerungen) hinzufügen
    elif event['type'] == 'invoice.payment_succeeded':
        invoice = event['data']['object']
        await handle_invoice_payment_succeeded(invoice)

    return {"status": "success"}

async def handle_subscription_update(subscription):
    db = SessionLocal() 
    try:
        # 1. Tenant finden (Robust über Metadata oder CustomerID)
        tenant_id = subscription.get('metadata', {}).get('tenant_id')
        tenant = None
        
        if tenant_id:
            tenant = db.query(models.Tenant).filter(models.Tenant.id == int(tenant_id)).first()
            
        if not tenant:
            customer_id = subscription.get('customer')
            if customer_id:
                tenant = db.query(models.Tenant).filter(models.Tenant.stripe_customer_id == customer_id).first()
        
        if tenant:
            # WICHTIG: Nutze die zentrale Logik aus stripe_service!
            # Diese Funktion schreibt Status, Plan UND die "Next Payment" Infos in die DB.
            stripe_service.update_tenant_from_subscription(db, tenant, subscription)
            print(f"Webhook success: Tenant {tenant.id} updated via service logic")
        else:
            print(f"Webhook warning: Tenant not found for subscription {subscription.get('id')}")
            
    except Exception as e:
        print(f"Webhook Error: {e}")
    finally:
        db.close()


async def handle_subscription_deleted(subscription):
    db = SessionLocal()
    try:
        customer_id = subscription.get('customer')
        tenant = db.query(models.Tenant).filter(models.Tenant.stripe_customer_id == customer_id).first()
        
        if tenant:
            tenant.plan = 'starter' 
            tenant.subscription_ends_at = datetime.now(timezone.utc)
            tenant.stripe_subscription_status = 'canceled'
            tenant.cancel_at_period_end = False 
            
            db.commit()
            print(f"Webhook: Subscription deleted for tenant {tenant.name}")
    finally:
        db.close()


async def handle_invoice_payment_succeeded(invoice):
    """
    Wird aufgerufen, wenn eine Rechnung erfolgreich bezahlt wurde.
    Lädt die Subscription, extrahiert das korrekte Enddatum (auch aus Items) 
    und erzwingt das Update.
    """
    subscription_id = get_subscription_id_safe(invoice)

    if subscription_id:
        print(f"DEBUG: Subscription ID gefunden: {subscription_id}")
        try:
            # 1. Subscription von Stripe laden
            subscription = stripe.Subscription.retrieve(subscription_id)
            
            # --- PATCH START: Datum aus Items holen ---
            # Das übergebene Objekt hat 'current_period_end' nicht im Root, aber in items.data[0]
            current_end = subscription.get('current_period_end')
            
            # Wenn Root-Datum fehlt oder null ist, suchen wir in den Items
            if not current_end:
                print("DEBUG: 'current_period_end' fehlt im Root. Suche in Items...")
                items = subscription.get('items', {})
                if items and hasattr(items, 'data'):
                    # Wir nehmen das weiteste Enddatum aller Items
                    max_end = 0
                    for item in items.data:
                        item_end = item.get('current_period_end')
                        if item_end and item_end > max_end:
                            max_end = item_end
                    
                    if max_end > 0:
                        print(f"DEBUG: Datum aus Items extrahiert: {max_end}")
                        # Wir patchen das Objekt, damit stripe_service es versteht
                        subscription['current_period_end'] = max_end
                        current_end = max_end

            # --- PATCH ENDE ---

            # 2. Datum aus der Invoice als zusätzlicher Fallback/Override prüfen
            # (Falls die Invoice "neuer" ist als das Subscription-Objekt)
            invoice_period_end = 0
            lines = invoice.get('lines', {})
            if lines and isinstance(lines, dict) and 'data' in lines:
                for item in lines['data']:
                    if item.get('period') and item['period'].get('end'):
                        end_ts = item['period']['end']
                        if end_ts > invoice_period_end:
                            invoice_period_end = end_ts
            
            # 3. Wenn die Rechnung ein noch neueres Datum hat als das (ggf. gepatchte) Subscription-Objekt
            if invoice_period_end > (current_end or 0):
                print(f"DEBUG: Invoice hat neueres Enddatum ({invoice_period_end}) als Subscription ({current_end}). Nutze Invoice-Datum.")
                subscription['current_period_end'] = invoice_period_end
            
            # 4. Bestehende Update-Logik aufrufen
            # stripe_service erwartet subscription.current_period_end oder subscription['current_period_end']
            await handle_subscription_update(subscription)
            
            print(f"Webhook: Invoice payment processed. Period End set to: {subscription.get('current_period_end')}")
            
        except Exception as e:
            print(f"Webhook Error handling invoice payment: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("DEBUG: Keine Subscription ID gefunden (evtl. Einmalzahlung). Skipping.")


# --- STRIPE INTEGRATION ---

@app.post("/api/stripe/create-subscription")
def create_subscription(
    data: schemas.SubscriptionUpdate,
    cycle: str = "monthly",
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication failed")
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
        
    return stripe_service.create_checkout_session(db, tenant.id, data.plan, cycle, current_user.email)

@app.post("/api/stripe/cancel")
def cancel_subscription_endpoint(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    return stripe_service.cancel_subscription(db, tenant.id)

@app.get("/api/stripe/details", response_model=Optional[schemas.SubscriptionDetails])
def get_subscription_details_endpoint(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    return stripe_service.get_subscription_details(db, tenant.id)

@app.get("/api/stripe/portal")
def get_portal_url(
    return_url: str,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    return stripe_service.get_billing_portal_url(db, tenant.id, return_url)

@app.get("/api/stripe/invoices", response_model=List[schemas.Invoice])
def get_invoices_endpoint(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    return stripe_service.get_invoices(db, tenant.id)

@app.post("/api/newsletter/subscribe", response_model=schemas.NewsletterSubscriber)
def subscribe_to_newsletter(data: schemas.NewsletterSubscriberCreate, db: Session = Depends(get_db)):
    return crud.add_newsletter_subscriber(db, data.email, data.source)

@app.post("/api/tenants/register", response_model=schemas.Tenant)
def register_tenant(tenant_data: schemas.TenantCreate, admin_data: schemas.UserCreate, db: Session = Depends(get_db)):
    if crud.get_tenant_by_subdomain(db, tenant_data.subdomain):
        raise HTTPException(status_code=400, detail="Subdomain already taken")
    
    trial_end = datetime.now(timezone.utc) + timedelta(days=14)
    new_tenant = models.Tenant(
        name=tenant_data.name,
        subdomain=tenant_data.subdomain,
        support_email=tenant_data.support_email,
        plan="enterprise",
        config=tenant_data.config.model_dump(),
        subscription_ends_at=trial_end,
        is_active=True
    )
    db.add(new_tenant)
    db.commit()
    db.refresh(new_tenant)
    
    try:
        crud.add_newsletter_subscriber(db, admin_data.email, "school_registration")
    except: pass

    auth_id = None
    try:
        if not admin_data.password: admin_data.password = secrets.token_urlsafe(16)
        redirect_url = f"https://{tenant_data.subdomain}.pfotencard.de/auth/callback"
        metadata = {
            "branding_name": "Pfotencard",
            "branding_logo": "https://pfotencard.de/logo.png",
            "branding_color": "#22C55E",
            "school_name": "Pfotencard"
        }
        auth_res = supabase.auth.sign_up({
            "email": admin_data.email,
            "password": admin_data.password,
            "options": {"data": metadata, "email_redirect_to": redirect_url}
        })
        if auth_res.user: auth_id = auth_res.user.id
    except Exception as e:
        print(f"Supabase error: {e}")

    admin_data.role = "admin"
    crud.create_user(db, admin_data, new_tenant.id, auth_id=auth_id)
    return new_tenant

@app.post("/api/register", response_model=schemas.User)
def register_user(user: schemas.UserCreate, db: Session = Depends(get_db), tenant: models.Tenant = Depends(auth.get_current_tenant)):
    db_user = crud.get_user_by_email(db, email=user.email, tenant_id=tenant.id)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered in this school")
    return crud.create_user(db=db, user=user, tenant_id=tenant.id, auth_id=str(user.auth_id) if user.auth_id else None)

@app.post("/api/users", response_model=schemas.User)
def create_user(
    user: schemas.UserCreate, 
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    db_user = crud.get_user_by_email(db, email=user.email, tenant_id=tenant.id)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered in this school")
    
    auth_id = None
    try:
        tenant_branding = tenant.config.get("branding", {})
        metadata = {
            "branding_name": tenant.name,
            "branding_logo": tenant_branding.get("logo_url") or "https://pfotencard.de/logo.png",
            "branding_color": tenant_branding.get("primary_color") or "#22C55E",
            "school_name": tenant.name
        }
        try:
            auth_res = supabase.auth.admin.invite_user_by_email(
                user.email,
                {"data": metadata, "redirectTo": f"https://{tenant.subdomain}.pfotencard.de/update-password"}
            )
            if auth_res.user: auth_id = auth_res.user.id
        except Exception:
            users_res = supabase.auth.admin.list_users()
            existing = next((u for u in users_res.data.users if u.email == user.email), None)
            if existing:
                auth_id = existing.id
                supabase.auth.admin.update_user_by_id(auth_id, {"user_metadata": metadata})
    except Exception as e:
        print(f"Supabase User Sync failed: {e}")

    return crud.create_user(db=db, user=user, tenant_id=tenant.id, auth_id=auth_id)

@app.get("/api/users/staff", response_model=List[schemas.User])
def read_staff_users(
    current_user: schemas.User = Depends(auth.get_current_active_user),
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter', 'customer', 'kunde']:
         raise HTTPException(status_code=403, detail="Not authorized")
    staff = db.query(models.User).filter(
        models.User.tenant_id == tenant.id,
        models.User.role.in_(['admin', 'mitarbeiter']),
        models.User.is_active == True
    ).all()
    return staff

@app.get("/api/users", response_model=List[schemas.User])
def read_users(
    skip: int = 0, limit: int = 100, db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.get_users(db, tenant.id, skip=skip, limit=limit)

@app.get("/api/users/by-auth/{auth_id}", response_model=schemas.User)
def read_user_by_auth(
    auth_id: str, db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    db_user = crud.get_user_by_auth_id(db, auth_id, tenant.id)
    if not db_user: raise HTTPException(status_code=404, detail="User not found")
    if current_user.role in ['admin', 'mitarbeiter'] or current_user.auth_id == auth_id:
        return db_user
    raise HTTPException(status_code=403, detail="Not authorized")

@app.get("/api/public/users/{auth_id}", response_model=schemas.User)
def read_user_public(auth_id: str, db: Session = Depends(get_db), tenant: models.Tenant = Depends(auth.get_current_tenant)):
    db_user = crud.get_user_by_auth_id(db, auth_id, tenant.id)
    if not db_user: raise HTTPException(status_code=404, detail="User not found")
    if db_user.role not in ['customer', 'kunde']: raise HTTPException(status_code=403, detail="Not authorized")
    return db_user

@app.get("/api/users/{user_id}", response_model=schemas.User)
def read_user(
    user_id: int, db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    db_user = crud.get_user(db, user_id, tenant.id)
    if not db_user: raise HTTPException(status_code=404, detail="User not found")
    if current_user.role in ['admin', 'mitarbeiter'] or current_user.id == user_id:
        return db_user
    raise HTTPException(status_code=403, detail="Not authorized")

@app.put("/api/users/{user_id}", response_model=schemas.User)
def update_user_endpoint(
    user_id: int, user_update: schemas.UserUpdate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.id != user_id and current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    updated = crud.update_user(db, user_id, tenant.id, user_update)
    if not updated: raise HTTPException(status_code=404, detail="User not found")
    if user_update.password and updated.auth_id:
        try:
            supabase.auth.admin.update_user_by_id(str(updated.auth_id), {"password": user_update.password})
        except: pass
    return updated

@app.put("/api/users/{user_id}/status", response_model=schemas.User)
def update_user_status(
    user_id: int, status: schemas.UserStatusUpdate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.update_user_status(db, user_id, tenant.id, status)

@app.put("/api/users/{user_id}/level", response_model=schemas.User)
def manual_level_up(
    user_id: int, level_update: schemas.UserLevelUpdate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.update_user_level(db, user_id, level_update.level_id)

@app.post("/api/users/{user_id}/level-up", response_model=schemas.User)
def perform_level_up_endpoint(
    user_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.perform_level_up(db, user_id, tenant.id)

@app.post("/api/transactions", response_model=schemas.Transaction)
def create_transaction(
    transaction: schemas.TransactionCreate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
         raise HTTPException(status_code=403, detail="Not authorized")
    return crud.create_transaction(db, transaction, current_user.id, tenant.id)

@app.get("/api/transactions", response_model=List[schemas.Transaction])
def read_transactions(
    user_id: Optional[int] = None, db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    query = db.query(models.Transaction).filter(models.Transaction.tenant_id == tenant.id)
    if current_user.role in ['kunde', 'customer']:
        query = query.filter(models.Transaction.user_id == current_user.id)
    elif current_user.role in ['mitarbeiter', 'staff'] and not user_id:
        query = query.filter(models.Transaction.booked_by_id == current_user.id)
    elif user_id:
        query = query.filter(models.Transaction.user_id == user_id)
    return query.order_by(models.Transaction.date.desc()).all()

@app.put("/api/dogs/{dog_id}", response_model=schemas.Dog)
def update_dog(
    dog_id: int, dog: schemas.DogBase, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    db_dog = crud.get_dog(db, dog_id, tenant.id)
    if not db_dog: raise HTTPException(404, "Dog not found")
    if current_user.role not in ['admin', 'mitarbeiter'] and db_dog.owner_id != current_user.id:
        raise HTTPException(403, "Not authorized")
    return crud.update_dog(db, dog_id, tenant.id, dog)

@app.delete("/api/dogs/{dog_id}")
def delete_dog(
    dog_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(403, "Not authorized")
    
    # 1. DB Löschen (Gibt Pfad zurück)
    result = crud.delete_dog(db, dog_id, tenant.id)
    if not result:
        raise HTTPException(404, "Dog not found")
        
    # 2. Storage Cleanup (Bild löschen)
    if result.get("image_path"):
        delete_file_from_storage(supabase, "public_uploads", result["image_path"])
        
    return {"ok": True}


@app.post("/api/users/{user_id}/documents", response_model=schemas.Document)
async def upload_document(
    user_id: int, upload_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    file_content = await upload_file.read()
    file_path_in_bucket = f"{tenant.id}/{user_id}/{upload_file.filename}"
    try:
        supabase.storage.from_("documents").upload(
            path=file_path_in_bucket, file=file_content,
            file_options={"content-type": upload_file.content_type, "upsert": "true"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
    return crud.create_document(db, user_id, tenant.id, upload_file.filename, upload_file.content_type, file_path_in_bucket)

@app.get("/api/documents/{document_id}")
def read_document(
    document_id: int, db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    doc = crud.get_document(db, document_id, tenant.id)
    if not doc: raise HTTPException(404, "Document not found")
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != doc.user_id:
        raise HTTPException(403, "Not authorized")
    try:
        res = supabase.storage.from_("documents").create_signed_url(doc.file_path, 60)
        return {"url": res["signedURL"]}
    except Exception: raise HTTPException(404, "File not found")

@app.delete("/api/documents/{document_id}")
def delete_document(
    document_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    doc = crud.get_document(db, document_id, tenant.id)
    if not doc: raise HTTPException(404, "Document not found")
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != doc.user_id:
        raise HTTPException(403, "Not authorized")
    
    # 1. DB Löschen (Gibt Pfad zurück)
    result = crud.delete_document(db, document_id, tenant.id)
    
    # 2. Storage Cleanup
    if result and result.get("file_path"):
        delete_file_from_storage(supabase, "documents", result["file_path"])
        
    return {"ok": True}


@app.post("/api/upload/image")
async def upload_public_image(
    file: UploadFile = File(...), db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    file_ext = os.path.splitext(file.filename)[1]
    safe_name = f"{tenant.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{file_ext}"
    file_content = await file.read()
    try:
        supabase.storage.from_("public_uploads").upload(
            path=safe_name, file=file_content,
            file_options={"content-type": file.content_type, "upsert": "true"}
        )
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
    return {"url": f"{settings.SUPABASE_URL}/storage/v1/object/public/public_uploads/{safe_name}"}

@app.post("/api/appointments", response_model=schemas.Appointment)
def create_appointment(
    appointment: schemas.AppointmentCreate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    return crud.create_appointment(db, appointment, tenant.id)

@app.put("/api/appointments/{appointment_id}", response_model=schemas.Appointment)
def update_appointment(
    appointment_id: int, appointment: schemas.AppointmentUpdate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    updated = crud.update_appointment(db, appointment_id, tenant.id, appointment)
    if not updated: raise HTTPException(status_code=404, detail="Appointment not found")
    return updated

@app.delete("/api/appointments/{appointment_id}")
def delete_appointment(
    appointment_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    success = crud.delete_appointment(db, appointment_id, tenant.id)
    if not success: raise HTTPException(status_code=404, detail="Appointment not found")
    return {"ok": True}

@app.get("/api/appointments", response_model=List[schemas.Appointment])
def read_appointments(
    db: Session = Depends(get_db), tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.get_appointments(db, tenant.id)

@app.post("/api/appointments/{appointment_id}/book", response_model=schemas.Booking)
def book_appointment(
    appointment_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.create_booking(db, tenant.id, appointment_id, current_user.id)

@app.get("/api/users/me/bookings", response_model=List[schemas.Booking])
def read_my_bookings(
    db: Session = Depends(get_db), tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.get_user_bookings(db, tenant.id, current_user.id)

@app.delete("/api/appointments/{appointment_id}/book")
def cancel_appointment_booking(
    appointment_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # Rückgabetyp ist jetzt ein Dict, kein Schema mehr erzwingen oder Schema anpassen
    return crud.cancel_booking(db, tenant.id, appointment_id, current_user.id)

@app.get("/api/appointments/{appointment_id}/participants", response_model=List[schemas.Booking])
def read_participants(
    appointment_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    return crud.get_participants(db, tenant.id, appointment_id)

@app.put("/api/bookings/{booking_id}/attendance", response_model=schemas.Booking)
def toggle_booking_attendance(
    booking_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    return crud.toggle_attendance(db, tenant.id, booking_id)

@app.post("/api/news/upload-image")
async def upload_news_image(
    upload_file: UploadFile = File(...), db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    file_content = await upload_file.read()
    file_ext = os.path.splitext(upload_file.filename)[1]
    safe_name = f"{int(datetime.now().timestamp())}_{secrets.token_hex(4)}{file_ext}"
    file_path = f"{tenant.id}/news/{safe_name}"
    try:
        supabase.storage.from_("documents").upload(path=file_path, file=file_content, file_options={"content-type": upload_file.content_type, "upsert": "true"})
    except Exception as e: raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
    return {"url": supabase.storage.from_("documents").get_public_url(file_path)}

@app.post("/api/news", response_model=schemas.NewsPost)
def create_news(
    post: schemas.NewsPostCreate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    return crud.create_news_post(db, post, current_user.id, tenant.id)

@app.put("/api/news/{post_id}", response_model=schemas.NewsPost)
def update_news(
    post_id: int, post: schemas.NewsPostUpdate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    updated = crud.update_news_post(db, post_id, tenant.id, post)
    if not updated: raise HTTPException(status_code=404, detail="News post not found")
    return updated

@app.delete("/api/news/{post_id}")
def delete_news(
    post_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    success = crud.delete_news_post(db, post_id, tenant.id)
    if not success: raise HTTPException(status_code=404, detail="News post not found")
    return {"ok": True}

@app.get("/api/news", response_model=List[schemas.NewsPost])
def read_news(
    skip: int = 0, limit: int = 50, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.get_news_posts(db, tenant.id, current_user, skip, limit)

@app.post("/api/chat", response_model=schemas.ChatMessage)
def send_chat_message(
    msg: schemas.ChatMessageCreate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.create_chat_message(db, msg, current_user.id, tenant.id)

@app.get("/api/chat/conversations", response_model=List[schemas.ChatConversation])
def get_conversations(current_user: schemas.User = Depends(auth.get_current_active_user), db: Session = Depends(get_db)):
    return crud.get_chat_conversations_for_user(db, current_user)

@app.get("/api/chat/{other_user_id}", response_model=List[schemas.ChatMessage])
def read_chat_history(
    other_user_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.get_chat_history(db, tenant.id, current_user.id, other_user_id)

@app.post("/api/chat/{other_user_id}/read")
def mark_chat_read(
    other_user_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    crud.mark_messages_as_read(db, tenant.id, current_user.id, other_user_id)
    return {"ok": True}