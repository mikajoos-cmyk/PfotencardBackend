import os
import shutil
from starlette.responses import FileResponse
from fastapi import Depends, FastAPI, HTTPException, status, UploadFile, File, Request
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timedelta, timezone

from . import crud, models, schemas, auth
from .database import engine, get_db
from .config import settings
from supabase import create_client, Client
import secrets

models.Base.metadata.create_all(bind=engine)

app = FastAPI()
UPLOADS_DIR = "uploads"
os.makedirs(UPLOADS_DIR, exist_ok=True)

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
    
    # 2. Admin in Supabase Auth anlegen
    auth_id = None
    try:
        # Sicherstellen, dass ein Passwort da ist
        if not admin_data.password:
            admin_data.password = secrets.token_urlsafe(16)

        auth_res = supabase.auth.admin.create_user({
            "email": admin_data.email,
            "password": admin_data.password,
            "email_confirm": True
        })
        if auth_res.user:
            auth_id = auth_res.user.id
    except Exception as e:
        print(f"DEBUG: Supabase Registration skipped or failed: {e}")

    admin_data.role = "admin"
    crud.create_user(db, admin_data, new_tenant.id, auth_id=auth_id)
    
    return new_tenant

# --- USERS ---
@app.post("/api/users", response_model=schemas.User)
def create_user(
    user: schemas.UserCreate, 
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Not authorized")

    db_user = crud.get_user_by_email(db, email=user.email, tenant_id=tenant.id)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered in this school")
    
    # In Supabase Auth anlegen
    auth_id = None
    try:
        # Passwort generieren falls nicht vorhanden
        if not user.password:
            user.password = secrets.token_urlsafe(16)

        auth_res = supabase.auth.admin.create_user({
            "email": user.email,
            "password": user.password,
            "email_confirm": True
        })
        if auth_res.user:
            auth_id = auth_res.user.id
    except Exception as e:
        print(f"DEBUG: Supabase User Registration failed: {e}")

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
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    for_staff = (current_user.role == 'mitarbeiter')
    return crud.get_transactions_for_user(db, current_user.id, tenant.id, for_staff)

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
def upload_document(
    user_id: int,
    upload_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != user_id:
        raise HTTPException(403, "Not authorized")

    file_path = os.path.join(UPLOADS_DIR, f"{tenant.id}_{upload_file.filename}")
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(upload_file.file, buffer)

    return crud.create_document(db, user_id, tenant.id, upload_file.filename, upload_file.content_type, file_path)

@app.get("/api/documents/{document_id}")
def read_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
    tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    doc = crud.get_document(db, document_id, tenant.id)
    if not doc or not os.path.exists(doc.file_path):
        raise HTTPException(404, "Document not found")
        
    if current_user.role not in ['admin', 'mitarbeiter'] and current_user.id != doc.user_id:
        raise HTTPException(403, "Not authorized")
        
    return FileResponse(path=doc.file_path, filename=doc.file_name)

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

    if os.path.exists(doc.file_path):
        os.remove(doc.file_path)
        
    crud.delete_document(db, document_id, tenant.id)
    return {"ok": True}

# --- PUBLIC IMAGE UPLOAD (Logos, Badges) ---
from fastapi.staticfiles import StaticFiles

PUBLIC_UPLOADS_DIR = "public_uploads"
os.makedirs(PUBLIC_UPLOADS_DIR, exist_ok=True)

app.mount("/static/uploads", StaticFiles(directory=PUBLIC_UPLOADS_DIR), name="public_uploads")

@app.post("/api/upload/image")
def upload_public_image(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
         raise HTTPException(status_code=403, detail="Not authorized")
         
    # Unique filename
    file_ext = os.path.splitext(file.filename)[1]
    safe_name = f"{tenant.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{file_ext}"
    file_path = os.path.join(PUBLIC_UPLOADS_DIR, safe_name)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Return URL (relative or absolute)
    # Assuming standard setup: /static/uploads/...
    return {"url": f"/static/uploads/{safe_name}"}
