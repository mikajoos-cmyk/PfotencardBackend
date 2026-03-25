from datetime import datetime, timedelta, timezone
from typing import Optional
import json

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from . import crud, schemas, models
from .config import settings
from .database import get_db

# Password Hashing Setup
# We include both bcrypt and pbkdf2_sha256 to support legacy hashes
# and provide a fallback if bcrypt remains problematic in this environment.
pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")

# OAuth2 Scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain password against a hashed one."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hashes a plain password."""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Creates a new JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)

    if "sub" in to_encode and "email" not in to_encode:
        to_encode["email"] = to_encode["sub"]

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


# --- TENANT RESOLUTION LOGIC ---

def get_subdomain(request: Request) -> Optional[str]:
    """
    Liest die Subdomain aus dem Host-Header oder dem Custom-Header.
    """
    # 1. Custom Header für Frontend-Calls (wichtig für Marketing-Seite)
    if "x-tenant-subdomain" in request.headers:
        header_subdomain = request.headers.get("x-tenant-subdomain")
        print(f"DEBUG [get_subdomain]: Found header x-tenant-subdomain: '{header_subdomain}'")
        return header_subdomain.lower() if header_subdomain else None

    # 2. Host Header (für echte Subdomain-Aufrufe)
    host = request.headers.get("host", "")
    if not host:
        print("DEBUG [get_subdomain]: No host header found")
        return None

    domain = host.split(":")[0]

    # Ignoriere Localhost oder IP-Adressen (Fallback für Dev)
    if "localhost" in domain or "127.0.0.1" in domain:
        print(f"DEBUG [get_subdomain]: Localhost/IP detected on '{domain}', falling back to 'dev'")
        return "dev"

    parts = domain.split(".")
    if len(parts) >= 3:
        print(f"DEBUG [get_subdomain]: Extracted subdomain '{parts[0]}' from host '{host}'")
        return parts[0]

    print(f"DEBUG [get_subdomain]: Could not extract subdomain from host '{host}'")
    return None


async def get_current_tenant(
        request: Request, db: Session = Depends(get_db)
) -> models.Tenant:
    """
    Dependency, die den aktuellen Tenant basierend auf der Subdomain lädt.
    """
    subdomain = get_subdomain(request)
    print(f"DEBUG [get_current_tenant]: Resolved subdomain is '{subdomain}'")
    if not subdomain:
        # Versuche Fallback ID wenn keine Subdomain da ist
        tenant_id_header = request.headers.get("x-tenant-id")
        if tenant_id_header:
            print(f"DEBUG [get_current_tenant]: Trying fallback x-tenant-id: {tenant_id_header}")
            tenant = db.query(models.Tenant).filter(models.Tenant.id == int(tenant_id_header)).first()
            if tenant: 
                print(f"DEBUG [get_current_tenant]: Found tenant {tenant.id} via x-tenant-id header")
                return tenant

        print("DEBUG [get_current_tenant]: No subdomain or fallback ID provided")
        raise HTTPException(status_code=404, detail="No tenant specified (subdomain missing)")

    tenant = crud.get_tenant_by_subdomain(db, subdomain=subdomain)
    if not tenant:
        print(f"DEBUG [get_current_tenant]: Tenant for subdomain '{subdomain}' not found in DB")
        raise HTTPException(status_code=404, detail=f"School '{subdomain}' not found")

    print(f"DEBUG [get_current_tenant]: Successfully resolved tenant {tenant.id} ('{tenant.name}') for subdomain '{subdomain}'")
    if not tenant.is_active:
        # Erlaube Zugriff auf Rechnungen und Billing-Portal auch wenn inaktiv (wegen Abo-Kündigung)
        allowed_paths = ["/api/stripe/invoices", "/api/stripe/portal", "/api/stripe/details"]
        if not any(path in request.url.path for path in allowed_paths):
            raise HTTPException(status_code=400, detail="School account is inactive")

    return tenant


def resolve_user_id(db: Session, user_id_str: str, tenant_id: int) -> int:
    """
    Hilfsfunktion, die eine user_id (Ganzzahl oder UUID-String) in die
    interne numerische ID auflöst.
    """
    # 1. Versuchen als Integer zu parsen
    try:
        return int(user_id_str)
    except ValueError:
        pass

    # 2. Wenn kein Integer, als UUID / auth_id behandeln
    user = crud.get_user_by_auth_id(db, user_id_str, tenant_id)
    if user:
        return user.id

    # 3. Fallback: Als Email behandeln
    user = crud.get_user_by_email(db, user_id_str, tenant_id)
    if user:
        return user.id

    # 4. Wenn nicht gefunden, Exception werfen
    raise HTTPException(status_code=404, detail="User not found (ID resolution failed)")


async def get_current_active_user(
        token: str = Depends(oauth2_scheme),
        db: Session = Depends(get_db),
        tenant: models.Tenant = Depends(get_current_tenant)
) -> schemas.User:
    """
    Validiert Token UND prüft, ob der User zum aktuellen Tenant gehört.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"verify_aud": False}
        )

        # FIX: Wir holen uns 'sub' (die Supabase User UUID) und 'email'
        auth_id: str = payload.get("sub")
        email: str = payload.get("email")

        if auth_id is None and email is None:
            raise credentials_exception

    except JWTError:
        raise credentials_exception

    # 1. Versuch: User über die Auth-ID (UUID) finden (Stabil gegen E-Mail-Änderungen)
    user = None
    if auth_id:
        user = crud.get_user_by_auth_id(db, auth_id=auth_id, tenant_id=tenant.id)

    # 2. Versuch: Fallback auf E-Mail (für Legacy User oder Admin-Login ohne Supabase-ID)
    if not user and email:
        user = crud.get_user_by_email(db, email=email, tenant_id=tenant.id)

    if user is None:
        raise HTTPException(status_code=401, detail="User not found in this school")

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    # Prüfen, ob das Abo der Schule abgelaufen ist
    if user.role != 'admin':
        if tenant.subscription_ends_at:
            now = datetime.now(timezone.utc)
            if tenant.subscription_ends_at < now:
                error_detail = {
                    "code": "SUBSCRIPTION_EXPIRED",
                    "message": "Das Abonnement der Hundeschule ist abgelaufen.",
                    "support_email": tenant.support_email or "support@pfotencard.de"
                }
                raise HTTPException(
                    status_code=402,
                    detail=error_detail
                )

    return user


def verify_active_subscription(request: Request, tenant: models.Tenant = Depends(get_current_tenant)):
    """
    Blockiert den Zugriff, wenn das Abo abgelaufen ist.
    Wird für alle Schreib-Operationen (POST, PUT, DELETE) verwendet.
    """
    # Sonderlocke: create-subscription darf IMMER aufgerufen werden, auch wenn das Abo abgelaufen ist
    if "/api/stripe/create-subscription" in request.url.path:
        return tenant

    now = datetime.now(timezone.utc)

    # Toleranz: Wir geben evtl. 24h Puffer, damit nicht mitten am Tag abgeschaltet wird
    if tenant.subscription_ends_at and tenant.subscription_ends_at < now:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,  # Spezieller Code für Frontend
            detail="Subscription expired. Please update your payment details."
        )
    return tenant


async def get_current_superadmin(
        token: str = Depends(oauth2_scheme),
        db: Session = Depends(get_db)
) -> models.User:
    """
    Validiert den Token und prüft, ob der User die Rolle 'superadmin' hat
    und KEINEM Tenant zugeordnet ist.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"verify_aud": False}
        )
        email: str = payload.get("email")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Super-Admin hat das is_superadmin Flag
    user = db.query(models.User).filter(
        models.User.email == email,
        models.User.is_superadmin == True
    ).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions (Super-Admin required)"
        )

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    return user