import os
import shutil
from starlette.responses import FileResponse
from fastapi import Depends, FastAPI, HTTPException, status, UploadFile, File, Request
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from . import crud, models, schemas, auth
from .database import engine, get_db
from .config import settings
from supabase import create_client, Client
import secrets

models.Base.metadata.create_all(bind=engine)

app = FastAPI()

# Supabase Client initialisieren
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

origins_regex = r"https://(.*\.)?pfotencard\.de|http://localhost:\d+"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=origins_regex, # Hier Regex nutzen statt allow_origins
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "Pfotencard Multi-Tenant API is running"}

# --- CONFIG ENDPOINT ---
@app.get("/api/config", response_model=schemas.AppConfig)
def read_app_config(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    return crud.get_app_config(db, tenant.id)

# --- SETTINGS ENDPOINT (NEU) ---
@app.put("/api/settings")
def update_settings(
    settings: schemas.SettingsUpdate,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    # Nur Admins dürfen Einstellungen ändern
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
        
    crud.update_tenant_settings(db, tenant.id, settings)
    return {"message": "Settings updated successfully"}

# --- AUTHENTICATION ---
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
    
    return {
        "access_token": access_token, 
        "token_type": "bearer", 
        "user": user
    }

@app.get("/api/users/me", response_model=schemas.User)
async def read_users_me(current_user: schemas.User = Depends(auth.get_current_active_user)):
    return current_user

# --- TENANT STATUS & SUBSCRIPTION ---

@app.get("/api/tenants/status", response_model=schemas.TenantStatus)
def check_tenant_status(subdomain: str, db: Session = Depends(get_db)):
    tenant = crud.get_tenant_by_subdomain(db, subdomain)
    if not tenant:
        return {"exists": False}
    
    is_valid = True
    if tenant.subscription_ends_at and tenant.subscription_ends_at < datetime.now(timezone.utc):
        is_valid = False
        
    return {
        "exists": True, 
        "name": tenant.name,
        "subscription_valid": is_valid,
        "subscription_ends_at": tenant.subscription_ends_at,
        "plan": tenant.plan
    }

@app.post("/api/tenants/subscribe")
def update_subscription(data: schemas.SubscriptionUpdate, db: Session = Depends(get_db)):
    tenant = crud.get_tenant_by_subdomain(db, data.subdomain)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    
    tenant.plan = data.plan
    tenant.subscription_ends_at = datetime.now(timezone.utc) + timedelta(days=365)
    tenant.is_active = True
    
    db.add(tenant)
    db.commit()
    return {"message": "Subscription updated successfully", "valid_until": tenant.subscription_ends_at}


# --- NEWSLETTER ENDPOINT (Öffentlich für Marketing-Seite) ---

@app.post("/api/newsletter/subscribe", response_model=schemas.NewsletterSubscriber)
def subscribe_to_newsletter(
    data: schemas.NewsletterSubscriberCreate,
    db: Session = Depends(get_db)
):
    """
    Fügt eine E-Mail zur globalen Marketing-Liste hinzu.
    """
    return crud.add_newsletter_subscriber(db, data.email, data.source)

# --- TENANT REGISTRATION ---
@app.post("/api/tenants/register", response_model=schemas.Tenant)
def register_tenant(
    tenant_data: schemas.TenantCreate, 
    admin_data: schemas.UserCreate,
    db: Session = Depends(get_db)
):
    if crud.get_tenant_by_subdomain(db, tenant_data.subdomain):
        raise HTTPException(status_code=400, detail="Subdomain already taken")
        
    trial_end = datetime.now(timezone.utc) + timedelta(days=14)
    
    # 1. Tenant erstellen
    new_tenant = models.Tenant(
        name=tenant_data.name,
        subdomain=tenant_data.subdomain,
        plan=tenant_data.plan,
        config=tenant_data.config.model_dump(),
        subscription_ends_at=trial_end
    )
    db.add(new_tenant)
    db.commit()
    db.refresh(new_tenant)
    
    # --- NEU: Hundeschule (Admin) automatisch zum Newsletter hinzufügen ---
    # Wir fangen Fehler ab, damit die Registrierung nicht scheitert, nur weil der Newsletter-Eintrag fehlschlägt
    try:
        crud.add_newsletter_subscriber(db, admin_data.email, "school_registration")
    except Exception as e:
        print(f"Warnung: Konnte Admin nicht zum Newsletter hinzufügen: {e}")
    # ---------------------------------------------------------------------

    # 2. Admin in Supabase Auth anlegen
    auth_id = None
    try:
        if not admin_data.password:
            admin_data.password = secrets.token_urlsafe(16)

        # --- DEBUG LOG SUPABASE ---
        print("DEBUG: Starte Supabase Sign Up für Admin...")
        redirect_url = f"https://{tenant_data.subdomain}.pfotencard.de/auth/callback"
        print(f"DEBUG: Redirect URL: {redirect_url}")
        
        metadata = {
            "branding_name": "Pfotencard",
            "branding_logo": "https://pfotencard.de/logo.png", # Sicherstellen dass das Bild existiert!
            "branding_color": "#22C55E",
            "school_name": "Pfotencard"
        }
        print(f"DEBUG: Metadata: {metadata}")
        # --- DEBUG LOG END ---

        auth_res = supabase.auth.sign_up({
            "email": admin_data.email,
            "password": admin_data.password,
            "options": {
                "data": metadata, # Hier werden die Daten übergeben!
                "email_redirect_to": redirect_url
            }
        })
        if auth_res.user:
            auth_id = auth_res.user.id
            print(f"DEBUG: Supabase User erstellt mit ID: {auth_id}")
            
    except Exception as e:
        print(f"DEBUG: FEHLER bei Supabase Registration: {e}")
    admin_data.role = "admin"
    crud.create_user(db, admin_data, new_tenant.id, auth_id=auth_id)
    
    return new_tenant

# --- USER REGISTRATION ---
@app.post("/api/register", response_model=schemas.User)
def register_user(
    user: schemas.UserCreate, 
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    print(23232323)
    db_user = crud.get_user_by_email(db, email=user.email, tenant_id=tenant.id)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered in this school")
    print(23235455435)
    return crud.create_user(db=db, user=user, tenant_id=tenant.id, auth_id=str(user.auth_id) if user.auth_id else None)

# --- USERS ---
@app.post("/api/users", response_model=schemas.User)
def create_user(
    user: schemas.UserCreate, 
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    print(23232342342423423)
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    print(33332333)
    db_user = crud.get_user_by_email(db, email=user.email, tenant_id=tenant.id)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered in this school")
    print(44444444)
    
    # In Supabase Auth anlegen
    auth_id = None
    try:
        tenant_branding = tenant.config.get("branding", {})
        branding_logo = tenant_branding.get("logo_url") or "https://pfotencard.de/logo.png"
        branding_color = tenant_branding.get("primary_color") or "#22C55E"
        
        metadata = {
            "branding_name": tenant.name,
            "branding_logo": branding_logo,
            "branding_color": branding_color,
            "school_name": tenant.name
        }
        print("--------------------------------------------------")
        print(f"DEBUG: Sende Invite für {user.email}")
        print(f"DEBUG: RedirectTo URL: https://{tenant.subdomain}.pfotencard.de/update-password")
        print(f"DEBUG: Metadata Payload: {metadata}")
        print("--------------------------------------------------")
        # Versuch 1: Einladen
        try:
            auth_res = supabase.auth.admin.invite_user_by_email(
                user.email,
                {
                    "data": metadata,
                    "redirectTo": f"https://{tenant.subdomain}.pfotencard.de/update-password"
                }
            )
            if auth_res.user:
                auth_id = auth_res.user.id
        except Exception as invite_error:
            # Fallback: Wenn User schon existiert, Auth-ID suchen und Metadaten updaten
            print(f"Invite failed (user likely exists): {invite_error}. Updating metadata...")
            
            # User ID suchen
            users_res = supabase.auth.admin.list_users()
            existing_user = next((u for u in users_res.data.users if u.email == user.email), None) # Access .data.users
            
            if existing_user:
                auth_id = existing_user.id
                # Metadaten zwingend aktualisieren (für Branding-Wechsel)
                supabase.auth.admin.update_user_by_id(
                    auth_id,
                    {"user_metadata": metadata}
                )
            else:
                raise invite_error

    except Exception as e:
        print(f"DEBUG: Supabase User Sync failed completely: {e}")
        # Hier evtl. Fehler werfen, damit kein 'toter' lokaler User entsteht
        # raise HTTPException(status_code=500, detail="Could not create authentication user")

    return crud.create_user(db=db, user=user, tenant_id=tenant.id, auth_id=auth_id)

@app.get("/api/users", response_model=List[schemas.User])
def read_users(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")

    return crud.get_users(db, tenant.id, skip=skip, limit=limit)

@app.get("/api/users/by-auth/{auth_id}", response_model=schemas.User)
def read_user_by_auth(
    auth_id: str,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    db_user = crud.get_user_by_auth_id(db, auth_id, tenant.id)
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    if current_user.role in ['admin', 'mitarbeiter'] or current_user.auth_id == auth_id:
        return db_user
    
    raise HTTPException(status_code=403, detail="Not authorized")

# Public endpoint for QR code access (no authentication required)
@app.get("/api/public/users/{auth_id}", response_model=schemas.User)
def read_user_public(
    auth_id: str,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    """
    Public endpoint to access customer data via QR code without authentication.
    Uses auth_id (UUID) for better security.
    Only returns basic customer information.
    """
    db_user = crud.get_user_by_auth_id(db, auth_id, tenant.id)
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Only allow access to customer accounts, not admin/staff
    if db_user.role not in ['customer', 'kunde']:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return db_user

@app.get("/api/users/{user_id}", response_model=schemas.User)
def read_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    db_user = crud.get_user(db, user_id, tenant.id)
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    if current_user.role in ['admin', 'mitarbeiter'] or current_user.id == user_id:
        return db_user
    
    raise HTTPException(status_code=403, detail="Not authorized")

@app.put("/api/users/{user_id}", response_model=schemas.User)
def update_user_endpoint(
    user_id: int,
    user_update: schemas.UserUpdate,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.id != user_id and current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
        
    updated = crud.update_user(db, user_id, tenant.id, user_update)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Passwort in Supabase synchronisieren, falls geändert
    if user_update.password and updated.auth_id:
        try:
            supabase.auth.admin.update_user_by_id(
                str(updated.auth_id),
                {"password": user_update.password}
            )
        except Exception as e:
            print(f"DEBUG: Supabase Password Sync failed: {e}")
            
    return updated

@app.put("/api/users/{user_id}/status", response_model=schemas.User)
def update_user_status(
    user_id: int,
    status: schemas.UserStatusUpdate,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
        
    return crud.update_user_status(db, user_id, tenant.id, status)

@app.put("/api/users/{user_id}/level", response_model=schemas.User)
def manual_level_up(
    user_id: int,
    level_update: schemas.UserLevelUpdate,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return crud.update_user_level(db, user_id, level_update.level_id)

# --- PROPER LEVEL UP ENDPOINT ---
@app.post("/api/users/{user_id}/level-up", response_model=schemas.User)
def perform_level_up_endpoint(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Calls the logic that validates requirements and consumes achievements
    return crud.perform_level_up(db, user_id, tenant.id)

# --- TRANSACTIONS ---
@app.post("/api/transactions", response_model=schemas.Transaction)
def create_transaction(
    transaction: schemas.TransactionCreate,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
         raise HTTPException(status_code=403, detail="Not authorized")
         
    return crud.create_transaction(db, transaction, current_user.id, tenant.id)

@app.get("/api/transactions", response_model=List[schemas.Transaction])
def read_transactions(
    user_id: Optional[int] = None, # NEU: Filter für spezifischen Kunden
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    query = db.query(models.Transaction).filter(models.Transaction.tenant_id == tenant.id)

    if current_user.role == 'kunde' or current_user.role == 'customer':
        # Kunden sehen NUR ihre eigenen
        query = query.filter(models.Transaction.user_id == current_user.id)
    
    elif current_user.role == 'mitarbeiter' or current_user.role == 'staff':
        if user_id:
             # Mitarbeiter schaut sich spezifischen Kunden an
             query = query.filter(models.Transaction.user_id == user_id)
        else:
             # Mitarbeiter Dashboard: Sieht nur was er selbst gebucht hat
             query = query.filter(models.Transaction.booked_by_id == current_user.id)
             
    elif current_user.role == 'admin':
        if user_id:
            # Admin filtert nach Kunde
            query = query.filter(models.Transaction.user_id == user_id)
        else:
            # Admin Dashboard: Sieht ALLES (kein Filter)
            pass

    return query.order_by(models.Transaction.date.desc()).all()

# --- DOGS ---
@app.put("/api/dogs/{dog_id}", response_model=schemas.Dog)
def update_dog(
    dog_id: int,
    dog: schemas.DogBase,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    db_dog = crud.get_dog(db, dog_id, tenant.id)
    if not db_dog: raise HTTPException(404, "Dog not found")
    
    if current_user.role not in ['admin', 'mitarbeiter'] and db_dog.owner_id != current_user.id:
        raise HTTPException(403, "Not authorized")
        
    return crud.update_dog(db, dog_id, tenant.id, dog)

@app.delete("/api/dogs/{dog_id}")
def delete_dog(
    dog_id: int,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(403, "Not authorized")
    
    return crud.delete_dog(db, dog_id, tenant.id)

# --- DOCUMENTS ---
@app.post("/api/users/{user_id}/documents", response_model=schemas.Document)
async def upload_document(  # <--- WICHTIG: 'async' hinzugefügt
    user_id: int,
    upload_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    # 1. Dateiinhalt lesen (jetzt erlaubt, da async)
    file_content = await upload_file.read()
    
    # 2. Pfad für Supabase Storage definieren
    file_path_in_bucket = f"{tenant.id}/{user_id}/{upload_file.filename}"

    # 3. Direkt zu Supabase hochladen (statt lokal speichern)
    try:
        supabase.storage.from_("documents").upload(
            path=file_path_in_bucket,
            file=file_content,
            file_options={"content-type": upload_file.content_type, "upsert": "true"}
        )
    except Exception as e:
        print(f"Upload Error: {e}")
        # Wenn der Upload fehlschlägt, brechen wir ab
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

    # 4. Datenbank-Eintrag erstellen (Speichert den Bucket-Pfad, nicht den lokalen Pfad)
    return crud.create_document(
        db, 
        user_id, 
        tenant.id, 
        upload_file.filename, 
        upload_file.content_type, 
        file_path_in_bucket
    )


@app.get("/api/documents/{document_id}")
def read_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    doc = crud.get_document(db, document_id, tenant.id)
    if not doc:
        raise HTTPException(404, "Document not found")
        
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != doc.user_id:
        raise HTTPException(403, "Not authorized")
        
    # Signierte URL von Supabase holen (gültig für 60 Sekunden)
    try:
        res = supabase.storage.from_("documents").create_signed_url(doc.file_path, 60)
        # ÄNDERUNG: Wir geben die URL als JSON zurück, statt direkt umzuleiten
        return {"url": res["signedURL"]}
    except Exception as e:
         raise HTTPException(404, "File not found in storage")

@app.delete("/api/documents/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    doc = crud.get_document(db, document_id, tenant.id)
    if not doc: raise HTTPException(404, "Document not found")
    
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != doc.user_id:
        raise HTTPException(403, "Not authorized")

    if doc.file_path:
        supabase.storage.from_("documents").remove([doc.file_path])
    
    crud.delete_document(db, document_id, tenant.id)
    return {"ok": True}

# --- PUBLIC IMAGE UPLOAD (Logos, Badges) ---
from fastapi.staticfiles import StaticFiles

@app.post("/api/upload/image")
async def upload_public_image(  # <--- WICHTIG: 'async' hinzugefügt
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
         raise HTTPException(status_code=403, detail="Not authorized")
         
    file_ext = os.path.splitext(file.filename)[1]
    safe_name = f"{tenant.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{file_ext}"
    
    # 1. Inhalt lesen
    file_content = await file.read()
    
    # 2. Upload in 'public_uploads' Bucket
    try:
        supabase.storage.from_("public_uploads").upload(
            path=safe_name,
            file=file_content,
            file_options={"content-type": file.content_type, "upsert": "true"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage Error: {str(e)}")
        
    # 3. Öffentliche URL generieren
    project_url = settings.SUPABASE_URL
    public_url = f"{project_url}/storage/v1/object/public/public_uploads/{safe_name}"
    
    return {"url": public_url}

# --- APPOINTMENTS & BOOKINGS ---

@app.post("/api/appointments", response_model=schemas.Appointment)
def create_appointment(
    appointment: schemas.AppointmentCreate,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
         raise HTTPException(status_code=403, detail="Not authorized")
         
    return crud.create_appointment(db, appointment, tenant.id)

@app.get("/api/appointments", response_model=List[schemas.Appointment])
def read_appointments(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # Jeder eingeloggte User darf Termine sehen
    return crud.get_appointments(db, tenant.id)

@app.post("/api/appointments/{appointment_id}/book", response_model=schemas.Booking)
def book_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # Jeder Kunde darf buchen (evtl. Level-Checks hier später)
    return crud.create_booking(db, tenant.id, appointment_id, current_user.id)

@app.get("/api/users/me/bookings", response_model=List[schemas.Booking])
def read_my_bookings(
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    return crud.get_user_bookings(db, tenant.id, current_user.id)

@app.delete("/api/appointments/{appointment_id}/book")
def cancel_appointment_booking(
    appointment_id: int,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    # Eigene Buchung stornieren
    return crud.cancel_booking(db, tenant.id, appointment_id, current_user.id)

@app.get("/api/appointments/{appointment_id}/participants", response_model=List[schemas.Booking])
def read_participants(
    appointment_id: int,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
         raise HTTPException(status_code=403, detail="Not authorized")
         
    return crud.get_participants(db, tenant.id, appointment_id)

@app.put("/api/bookings/{booking_id}/attendance", response_model=schemas.Booking)
def toggle_booking_attendance(
    booking_id: int,
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
         raise HTTPException(status_code=403, detail="Not authorized")
         
    return crud.toggle_attendance(db, tenant.id, booking_id)

