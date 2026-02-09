# app/main.py
import os
import shutil
from starlette.responses import FileResponse
from fastapi import Depends, FastAPI, HTTPException, status, UploadFile, File, Request, Header
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import secrets
import stripe
import traceback

from . import crud, models, schemas, auth, stripe_service, legal, notification_service, invoice_service
from .storage_service import delete_file_from_storage, delete_folder_from_storage
from .database import engine, get_db, SessionLocal
from .config import settings
from supabase import create_client, Client

models.Base.metadata.create_all(bind=engine)
app = FastAPI()

supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

origins_regex = r"https://(.*\.)?pfotencard\.de|http://(localhost|127\.0\.0\.1):\d+"

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
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 1. Lokale Verifizierung versuchen
    if not auth.verify_password(form_data.password, user.hashed_password):
        # 2. Falls lokal falsch, gegen Supabase prüfen (Sync-Fallback)
        try:
            print(f"DEBUG: Local auth failed for {user.email}, trying Supabase fallback...")
            # Wir versuchen einen Supabase Login
            auth_res = supabase.auth.sign_in_with_password({
                "email": user.email,
                "password": form_data.password
            })
            
            if auth_res.user:
                # Login bei Supabase war erfolgreich! 
                # Wir aktualisieren das lokale Passwort, damit es beim nächsten Mal lokal klappt.
                print(f"DEBUG: Supabase auth success. Syncing password to local DB.")
                user.hashed_password = auth.get_password_hash(form_data.password)
                db.commit()
            else:
                # Auch Supabase sagt nein
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incorrect email or password",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        except Exception as e:
            print(f"DEBUG: Supabase fallback failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # WICHTIG: Nur Admins dürfen sich über diesen Endpoint (Marketing Webseite / API Login) anmelden.
    # Kunden und Mitarbeiter nutzen das App Frontend (Supabase Auth direkt).
    if user.role != 'admin':
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Zugriff verweigert: Nur Administratoren können sich hier anmelden."
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



@app.post("/api/auth/forgot-password")
async def forgot_password(
    data: schemas.ForgotPasswordRequest,
    db: Session = Depends(get_db)
):
    """
    Triggert den Passwort-Reset Prozess bei Supabase.
    Sendet eine E-Mail mit einem Link zur App des Tenants.
    """
    tenant = crud.get_tenant_by_subdomain(db, data.subdomain)
    if not tenant:
        # Falls subdomain falsch, können wir nichts tun. 
        # Wir geben trotzdem Erfolg vor, um das Enumeration-Risiko zu minimieren? 
        # Aber hier ist die Subdomain ja öffentlich bekannt.
        raise HTTPException(status_code=404, detail="Mandant nicht gefunden.")

    # Prüfen ob User in diesem Tenant existiert
    user = crud.get_user_by_email(db, email=data.email, tenant_id=tenant.id)
    if not user:
        # Sicherheit: Wir geben Erfolg zurück, auch wenn der User nicht existiert.
        return {"message": "Falls die E-Mail Adresse registriert ist, wurde ein Link versendet."}

    # Redirect URL zur Marketing-Webseite
    # (Dort wird der Recovery-Hash abgefangen und das Password-Change-Formular gezeigt)
    redirect_url = "https://pfotencard.de/anmelden"
    if "localhost" in settings.SUPABASE_URL or "127.0.0.1" in settings.SUPABASE_URL:
         redirect_url = "http://localhost:3000/anmelden" # Marketing Dev Port

    # Redirect URL zur Marketing-Webseite
    # (Dort wird der Recovery-Hash abgefangen und das Password-Change-Formular gezeigt)
    # WICHTIG: Diese URL muss im Supabase Dashboard unter Authentication -> URL Configuration -> Redirect URLs eingetragen sein!
    redirect_url = "https://pfotencard.de/anmelden"
    if "localhost" in settings.SUPABASE_URL or "127.0.0.1" in settings.SUPABASE_URL:
         redirect_url = "http://localhost:3000/anmelden" # Marketing Dev Port

    try:
        # Branding für die E-Mail aktualisieren (Marketing Logo & Farben)
        # Die Supabase E-Mail Templates müssen so konfiguriert sein, dass sie {{ .Data.branding_logo }} etc. nutzen
        if user.auth_id:
            try:
                metadata = {
                    "branding_name": "Pfotencard",
                    "branding_logo": "https://pfotencard.de/logo.png",
                    "branding_color": "#22C55E",
                    "school_name": "Pfotencard"
                }
                print(f"DEBUG: Updating user metadata for {user.auth_id}")
                supabase.auth.admin.update_user_by_id(
                    str(user.auth_id), 
                    {"user_metadata": metadata}
                )
            except Exception as meta_err:
                print(f"WARN: Metadata update failed: {meta_err}")
                # Wir machen weiter, auch wenn das Branding-Update fehlschlägt

        # Supabase reset_password_for_email aufrufen - Supabase schickt die Mail selbst
        print(f"DEBUG: Calling supabase.auth.reset_password_for_email for {data.email}")
        supabase.auth.reset_password_for_email(
            data.email,
            options={"redirect_to": redirect_url}
        )
    except Exception as e:
        print(f"CRITICAL: Supabase Reset Error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Supa-Fehler: {str(e)}")

    return {"message": "Falls die E-Mail Adresse registriert ist, wurde ein Link versendet."}
    


@app.post("/api/auth/reset-password")
async def reset_password(
    data: schemas.PasswordReset,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Proxy für den Passwort-Reset. 
    Erwartet den Supabase access_token im Authorization Header.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing access token")
    
    access_token = auth_header.split(" ")[1]

    try:
        # 1. Update bei Supabase
        # Wir müssen den Client mit dem Token des Users initialisieren oder admin nutzen?
        # reset_password_for_email erfordert, dass der User mit dem Token eingeloggt ist.
        # supabase.auth.set_session(access_token) # Nicht sicher ob das global gut ist
        
        # Sicherer: Admin update_user nutzen, aber wir brauchen die ID des Users
        # Wir holen den User-Context von Supabase
        user_res = supabase.auth.get_user(access_token)
        if not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        auth_id = user_res.user.id
        email = user_res.user.email

        # Supabase Password Update
        supabase.auth.admin.update_user_by_id(auth_id, {"password": data.password})

        # 2. Lokales Passwort synchronisieren
        # Wir suchen alle User mit dieser E-Mail über alle Tenants hinweg? 
        # Nein, am besten nur den, der zu dieser auth_id gehört (falls verknüpft).
        db_users = db.query(models.User).filter(models.User.auth_id == auth_id).all()
        for db_user in db_users:
            db_user.hashed_password = auth.get_password_hash(data.password)
        
        db.commit()

        return {"message": "Passwort erfolgreich aktualisiert."}

    except Exception as e:
        print(f"Reset Error: {e}")
        raise HTTPException(status_code=500, detail="Passwort konnte nicht aktualisiert werden.")



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
        return JSONResponse(status_code=400, content={"error": "Invalid payload"})
    except stripe.error.SignatureVerificationError:
        return JSONResponse(status_code=400, content={"error": "Invalid signature"})
    except Exception as e:
        print(f"Stripe Webhook Construction Error: {e}")
        return JSONResponse(status_code=500, content={"error": f"Webhook construction failed: {str(e)}"})

    try:
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

        # NEU: Handler für erfolgreiche Top-up Zahlungen
        elif event['type'] == 'payment_intent.succeeded':
            intent = event['data']['object']
            if intent.get('metadata', {}).get('type') == 'balance_topup':
                await handle_payment_intent_succeeded(intent)

        return {"status": "success"}
    except Exception as e:
        print(f"CRITICAL WEBHOOK ERROR [{event['type']}]: {e}")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": f"Internal error during event handling: {str(e)}"})

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


async def handle_payment_intent_succeeded(intent):
    db = SessionLocal()
    try:
        metadata = intent.get('metadata', {})
        
        user_id_str = metadata.get('user_id')
        tenant_id_str = metadata.get('tenant_id')
        amount_str = metadata.get('base_amount')
        bonus_str = metadata.get('bonus_amount')

        if not all([user_id_str, tenant_id_str, amount_str]):
            print(f"❌ Error: Missing metadata in PaymentIntent: {metadata}")
            return

        user_id = int(user_id_str)
        tenant_id = int(tenant_id_str)
        amount = float(amount_str)
        bonus = float(bonus_str) if bonus_str else 0.0

        # Transaktion erstellen (nutzt crud.create_transaction)
        # WICHTIG: crud.create_transaction macht bereits db.commit() am Ende!
        tx_data = schemas.TransactionCreate(
            user_id=user_id,
            type="Aufladung",
            description=f"Online-Aufladung via Stripe: {amount}€ + {bonus}€ Bonus",
            amount=amount
        )
        
        db_tx = crud.create_transaction(db, tx_data, booked_by_id=user_id, tenant_id=tenant_id)
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR in handle_payment_intent_succeeded: {e}")
        traceback.print_exc()
        # Wir werfen den Fehler NICHT hoch, damit der Webhook-Handler (stripe_webhook)
        # ggf. trotzdem eine strukturierte Antwort geben kann wenn gewünscht.
        # Aber da wir hier in einer async Task sind die von stripe_webhook aufgerufen wird, 
        # ist der 200 OK Response von stripe_webhook bereits gesendet oder wird gesendet?
        # Nö, stripe_webhook wartet darauf.
        raise e # Wir werfen es jetzt DOCH hoch, damit stripe_webhook es fängt!
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

@app.post("/api/stripe/create-topup-intent")
def create_topup_intent_endpoint(
    data: schemas.TopUpIntentCreate,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication failed")
    
    # Nutzer darf nur für sich selbst aufladen (oder Admin/Mitarbeiter für andere, aber hier Fokus auf Self-Service)
    # Da wir in 'auth.get_current_active_user' sind, haben wir den aktuellen User.
    return stripe_service.create_topup_intent(
        db, 
        user_id=current_user.id, 
        tenant_id=tenant.id, 
        amount=data.amount, 
        bonus=data.bonus
    )

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

@app.post("/api/settings/invoice-preview")
def preview_invoice_endpoint(
    settings: schemas.InvoiceSettings,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    
    branding = tenant.config.get("branding", {})
    branding_logo = branding.get("logo_url")
    
    pdf_buffer = invoice_service.generate_invoice_preview(settings.dict(), branding_logo_url=branding_logo)
    
    return StreamingResponse(
        pdf_buffer, 
        media_type="application/pdf"
    )

@app.get("/api/stripe/invoices", response_model=List[schemas.Invoice])
def get_invoices_endpoint(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    return stripe_service.get_invoices(db, tenant.id)

# NEU: Rechnungs-Download Endpoint (Platzhalter)
@app.get("/api/transactions/{transaction_id}/invoice")
def get_transaction_invoice(
    transaction_id: int,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # 1. Transaktion laden
    transaction = db.query(models.Transaction).filter(
        models.Transaction.id == transaction_id,
        models.Transaction.tenant_id == tenant.id
    ).first()
    
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    # 2. Berechtigungsprüfung: Nur Admin oder der User selbst
    if current_user.role != 'admin' and transaction.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this invoice")
        
    # 3. Prüfen ob eine Rechnungsnummer existiert
    if not transaction.invoice_number:
        raise HTTPException(status_code=404, detail="No invoice available for this transaction")

    pdf_buffer = invoice_service.generate_invoice_pdf(transaction, tenant, transaction.user)
    
    filename = f"Rechnung_{transaction.invoice_number}.pdf"
    
    return StreamingResponse(
        pdf_buffer, 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.post("/api/notifications/subscribe")
def subscribe_to_push(
    sub_data: schemas.PushSubscriptionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    """
    Speichert eine Push-Subscription für den aktuellen User.
    Vermeidet Duplikate durch Prüfung des Endpoints.
    """
    p256dh = sub_data.keys.get("p256dh")
    auth_key = sub_data.keys.get("auth")

    # Validierung: Keys sollten eine gewisse Mindestlänge haben
    # p256dh ist normalerweise ~87-88 chars, auth ist ~22-24 chars.
    if not p256dh or len(p256dh) < 20 or not auth_key or len(auth_key) < 10:
        print(f"WARN [Subscribe]: Ungültige Keys empfangen. p256dh len: {len(p256dh) if p256dh else 0}, auth len: {len(auth_key) if auth_key else 0}")
        raise HTTPException(status_code=400, detail="Invalid subscription keys")

    # Bestehende Subscription für diesen Endpoint suchen
    existing = db.query(models.PushSubscription).filter(
        models.PushSubscription.endpoint == sub_data.endpoint,
        models.PushSubscription.user_id == current_user.id
    ).first()
    
    if existing:
        # Falls vorhanden, p256dh und auth aktualisieren
        existing.p256dh = p256dh
        existing.auth = auth_key
    else:
        # Neu anlegen
        new_sub = models.PushSubscription(
            user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            endpoint=sub_data.endpoint,
            p256dh=p256dh,
            auth=auth_key
        )
        db.add(new_sub)
    
    db.commit()
    print(f"DEBUG [Subscribe]: Subscription erfolgreich für User {current_user.id} gespeichert. (p256dh len: {len(p256dh)})")
    return {"status": "success"}

@app.post("/api/notifications/test")
def test_notification(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    """
    Test-Endpoint für Admins, um push + email auszulösen.
    """
    if current_user.role not in ["admin", "employee", "mitarbeiter"]:
        raise HTTPException(status_code=403, detail="Only admins can test notifications")

    from .notification_service import notify_user
    
    notify_user(
        db=db,
        user_id=current_user.id,
        type="test",
        title="Test Benachrichtigung",
        message="Dies ist eine Test-Nachricht von PfotenCard.",
        url="/dashboard"
    )
    
    return {"status": "triggered"}

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
    # 1. Sicherheits-Check: Nur Admins/Mitarbeiter dürfen einladen
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # 2. Prüfen ob User bereits in der lokalen Datenbank dieser Schule existiert
    db_user = crud.get_user_by_email(db, email=user.email, tenant_id=tenant.id)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered in this school")
    
    auth_id = None
    
    # 3. Supabase Einladung senden
    try:
        # Branding Daten für das E-Mail Template
        tenant_branding = tenant.config.get("branding", {})
        logo_url = tenant_branding.get("logo_url") or "https://pfotencard.de/logo.png"
        primary_color = tenant_branding.get("primary_color") or "#22C55E"
        
        metadata = {
            "branding_name": tenant.name,
            "branding_logo": logo_url,
            "branding_color": primary_color,
            "school_name": tenant.name,
            "tenant_id": tenant.id
        }
        
        # --- KORREKTUR START ---
        # Wir entfernen "/update-password". 
        # Supabase leitet dann sauber auf die Subdomain weiter. 
        # App.tsx erkennt das Event und öffnet das Modal.
        redirect_url = f"https://{tenant.subdomain}.pfotencard.de/"
        # --- KORREKTUR ENDE ---

        print(f"DEBUG: Sende Invite an {user.email}...{redirect_url}")
        
        auth_res = supabase.auth.admin.invite_user_by_email(
            user.email,
            options={
                "data": metadata,
                "redirect_to": redirect_url
            }
        )
        
        if auth_res.user:
            auth_id = auth_res.user.id
            print(f"DEBUG: Invite erfolgreich. Auth ID: {auth_id}")

    except Exception as e:
        print(f"Supabase Invite Error: {e}")
        # Fallback: Wenn der User in Supabase global schon existiert (Fehler: "User already registered"),
        # müssen wir seine ID finden, um ihn lokal zu verknüpfen.
        try:
            # Wir suchen den User in Supabase
            users_res = supabase.auth.admin.list_users()
            existing_user = next((u for u in users_res.data.users if u.email == user.email), None)
            
            if existing_user:
                auth_id = existing_user.id
                print(f"DEBUG: User existierte bereits in Auth. ID übernommen: {auth_id}")
                
                # Optional: Metadaten aktualisieren, damit das Branding stimmt
                supabase.auth.admin.update_user_by_id(auth_id, {"user_metadata": metadata})
                
                # Optional: Da er schon existiert, bekommt er keine Invite-Mail von invite_user_by_email.
                # Man könnte hier manuell einen MagicLink senden, wenn man das möchte.
        except Exception as inner_e:
            print(f"Kritischer Fehler beim User-Lookup: {inner_e}")

    # 4. User in lokaler Datenbank anlegen (und mit Auth-ID verknüpfen)
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
    user_id: str, db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
    db_user = crud.get_user(db, resolved_id, tenant.id)
    if not db_user: raise HTTPException(status_code=404, detail="User not found")
    if current_user.role in ['admin', 'mitarbeiter'] or current_user.id == resolved_id:
        return db_user
    raise HTTPException(status_code=403, detail="Not authorized")

@app.put("/api/users/{user_id}", response_model=schemas.User)
def update_user_endpoint(
    user_id: str, user_update: schemas.UserUpdate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
    if current_user.id != resolved_id and current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    updated = crud.update_user(db, resolved_id, tenant.id, user_update)
    if not updated: raise HTTPException(status_code=404, detail="User not found")
    if user_update.password and updated.auth_id:
        try:
            supabase.auth.admin.update_user_by_id(str(updated.auth_id), {"password": user_update.password})
        except: pass
    return updated

@app.put("/api/users/{user_id}/status", response_model=schemas.User)
def update_user_status(
    user_id: str, status: schemas.UserStatusUpdate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
    return crud.update_user_status(db, resolved_id, tenant.id, status)
    
@app.delete("/api/users/{user_id}")
def delete_user_endpoint(
    user_id: str,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
    if current_user.id == resolved_id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")
    
    # Optional: Verhindern, dass Mitarbeiter Admins löschen
    db_user = crud.get_user(db, resolved_id, tenant.id)
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
        
    if current_user.role == 'mitarbeiter' and db_user.role == 'admin':
        raise HTTPException(status_code=403, detail="Employees cannot delete admins")

    # Sichern der Auth ID vor dem Löschen in der DB
    auth_id = db_user.auth_id

    success = crud.delete_user(db, resolved_id, tenant.id)
    if not success:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Aus Supabase Auth löschen
    if auth_id:
        try:
            supabase.auth.admin.delete_user(str(auth_id))
            print(f"DEBUG: User {auth_id} also deleted from Supabase Auth.")
        except Exception as e:
            print(f"Supabase Auth Delete Error: {e}")
            # Wir machen weiter, da der lokale User bereits weg ist
            
    return {"status": "success", "message": "User deleted successfully"}

@app.put("/api/users/{user_id}/level", response_model=schemas.User)
def manual_level_up(
    user_id: str, level_update: schemas.UserLevelUpdate, 
    dog_id: Optional[int] = None, # NEU
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
    if dog_id:
        dog = crud.get_dog(db, dog_id, tenant.id)
        if not dog or dog.owner_id != resolved_id:
             raise HTTPException(status_code=404, detail="Dog not found")
        dog.current_level_id = level_update.level_id
        db.add(dog)
        db.commit()
        db.refresh(dog)
        return crud.get_user(db, resolved_id, tenant.id)
        
    return crud.update_user_level(db, resolved_id, level_update.level_id)

@app.post("/api/users/{user_id}/level-up", response_model=schemas.User)
def perform_level_up_endpoint(
    user_id: str, 
    dog_id: Optional[int] = None, # NEU
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
    crud.perform_level_up(db, resolved_id, tenant.id, dog_id=dog_id)
    return crud.get_user(db, resolved_id, tenant.id)

@app.post("/api/users/{user_id}/dogs", response_model=schemas.Dog)
def create_dog_for_user(
    user_id: str, 
    dog: schemas.DogCreate, 
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != resolved_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.create_dog_for_user(db, dog, resolved_id, tenant.id)

@app.post("/api/transactions", response_model=schemas.Transaction)
def create_transaction(
    transaction: schemas.TransactionCreate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    if current_user.role not in ['admin', 'mitarbeiter']:
         raise HTTPException(status_code=403, detail="Not authorized")
    
    # NEU: user_id auflösen (kann ID oder UUID sein)
    resolved_id = auth.resolve_user_id(db, str(transaction.user_id), tenant.id)
    transaction.user_id = resolved_id
    
    return crud.create_transaction(db, transaction, current_user.id, tenant.id)

@app.get("/api/transactions", response_model=List[schemas.Transaction])
def read_transactions(
    user_id: Optional[str] = None, db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    query = db.query(models.Transaction).filter(models.Transaction.tenant_id == tenant.id)
    if current_user.role in ['kunde', 'customer']:
        query = query.filter(models.Transaction.user_id == current_user.id)
    elif current_user.role in ['mitarbeiter', 'staff'] and not user_id:
        query = query.filter(models.Transaction.booked_by_id == current_user.id)
    elif user_id:
        # user_id auflösen
        resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
        query = query.filter(models.Transaction.user_id == resolved_id)
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
        # Wir löschen hier aus "public_uploads", da dies der Bucket für öffentliche Bilder ist
        try:
            supabase.storage.from_("public_uploads").remove([result["image_path"]])
        except Exception:
            pass
        
    return {"ok": True}

@app.post("/api/dogs/{dog_id}/image", response_model=schemas.Dog)
async def upload_dog_image(
    dog_id: int, 
    upload_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    db_dog = crud.get_dog(db, dog_id, tenant.id)
    if not db_dog: raise HTTPException(404, "Dog not found")
    if current_user.role not in ['admin', 'mitarbeiter'] and db_dog.owner_id != current_user.id:
        raise HTTPException(403, "Not authorized")

    file_content = await upload_file.read()
    # Eindeutiger Pfad im public_uploads bucket
    file_extension = upload_file.filename.split('.')[-1] if '.' in upload_file.filename else 'jpg'
    file_path_in_bucket = f"dogs/{tenant.id}/{dog_id}_{int(datetime.now().timestamp())}.{file_extension}"
    
    try:
        # Vorheriges Bild löschen falls vorhanden
        if db_dog.image_url:
            try:
                supabase.storage.from_("public_uploads").remove([db_dog.image_url])
            except:
                pass

        supabase.storage.from_("public_uploads").upload(
            path=file_path_in_bucket, file=file_content,
            file_options={"content-type": upload_file.content_type, "upsert": "true"}
        )
        # Öffentliche URL abrufen
        res = supabase.storage.from_("public_uploads").get_public_url(file_path_in_bucket)
        public_url = res # get_public_url returns the string in newer versions or a dict in older ones. 
        # In this project, it seems to be used for logo_url too.
        
        # In der DB speichern wir den Pfad im Bucket, um ihn später löschen zu können, 
        # oder wir speichern die URL. Hier speichern wir den Pfad.
        db_dog.image_url = file_path_in_bucket
        db.commit()
        db.refresh(db_dog)
        return db_dog
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@app.post("/api/users/{user_id}/documents", response_model=schemas.Document)
async def upload_document(
    user_id: str, upload_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    resolved_id = auth.resolve_user_id(db, user_id, tenant.id)
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != resolved_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    file_content = await upload_file.read()
    file_path_in_bucket = f"{tenant.id}/{resolved_id}/{upload_file.filename}"
    try:
        supabase.storage.from_("documents").upload(
            path=file_path_in_bucket, file=file_content,
            file_options={"content-type": upload_file.content_type, "upsert": "true"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
    return crud.create_document(db, resolved_id, tenant.id, upload_file.filename, upload_file.content_type, file_path_in_bucket)

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

@app.post("/api/appointments/recurring", response_model=List[schemas.Appointment])
def create_recurring_appointments(
    appointment: schemas.AppointmentRecurringCreate, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']: raise HTTPException(status_code=403, detail="Not authorized")
    return crud.create_recurring_appointments(db, appointment, tenant.id)

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
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Session = Depends(get_db), tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.get_appointments(db, tenant.id, start_date=start_date, end_date=end_date)

@app.post("/api/appointments/{appointment_id}/book", response_model=schemas.Booking)
def book_appointment(
    appointment_id: int, 
    dog_id: Optional[int] = None, # NEU
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # crud.create_booking muss dog_id unterstützen
    return crud.create_booking(db, tenant.id, appointment_id, current_user.id, dog_id=dog_id)

@app.get("/api/users/me/bookings", response_model=List[schemas.Booking])
def read_my_bookings(
    db: Session = Depends(get_db), tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.get_user_bookings(db, tenant.id, current_user.id)

@app.delete("/api/appointments/{appointment_id}/book")
def cancel_appointment_booking(
    appointment_id: int, 
    dog_id: Optional[int] = None, # NEU
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # Rückgabetyp ist jetzt ein Dict, kein Schema mehr erzwingen oder Schema anpassen
    return crud.cancel_booking(db, tenant.id, appointment_id, current_user.id, dog_id=dog_id)

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
    return crud.toggle_attendance(db, tenant.id, booking_id, booked_by_id=current_user.id)

@app.post("/api/bookings/{booking_id}/bill")
def bill_booking_endpoint(
    booking_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.bill_booking(db, tenant.id, booking_id, booked_by_id=current_user.id)

@app.post("/api/appointments/{appointment_id}/bill-all")
def bill_all_appointment_participants(
    appointment_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.bill_all_participants(db, tenant.id, appointment_id, booked_by_id=current_user.id)

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

@app.get("/api/chat/{other_user_identifier}", response_model=List[schemas.ChatMessage])
def read_chat_history(
    other_user_identifier: str, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # Resolve identifier (could be ID or UUID)
    other_user_id = None
    if other_user_identifier.isdigit():
        other_user_id = int(other_user_identifier)
    else:
        # Try as UUID
        db_user = crud.get_user_by_auth_id(db, other_user_identifier, tenant.id)
        if db_user:
            other_user_id = db_user.id
            
    if not other_user_id:
        raise HTTPException(status_code=404, detail="User not found")
        
    return crud.get_chat_history(db, tenant.id, current_user.id, other_user_id)

@app.post("/api/chat/{other_user_identifier}/read")
def mark_chat_read(
    other_user_identifier: str, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # Resolve identifier
    other_user_id = None
    if other_user_identifier.isdigit():
        other_user_id = int(other_user_identifier)
    else:
        db_user = crud.get_user_by_auth_id(db, other_user_identifier, tenant.id)
        if db_user:
            other_user_id = db_user.id

    if not other_user_id:
        raise HTTPException(status_code=404, detail="User not found")

    crud.mark_messages_as_read(db, tenant.id, current_user.id, other_user_id)
    return {"ok": True}
@app.post("/api/appointments/{appointment_id}/grant-progress")
def grant_all_appointment_progress(
    appointment_id: int, db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.verify_active_subscription),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud.grant_all_progress(db, tenant.id, appointment_id)

    # In main.py hinzufügen
@app.get("/api/cron/reminders")
def trigger_reminders(
    x_cron_secret: str = Header(None), 
    db: Session = Depends(get_db)
):
    # Sicherheit: Prüfen ob der Aufruf berechtigt ist (z.B. Secret in .env)
    if x_cron_secret != settings.CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    count = crud.check_and_send_reminders(db)
    return {"status": "ok", "sent_reminders": count}
