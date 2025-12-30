from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from . import crud, schemas, models
from .config import settings
from .database import get_db

# Password Hashing Setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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
    header_subdomain = request.headers.get("x-tenant-subdomain")
    if header_subdomain:
        return header_subdomain.lower()

    # 2. Host Header (für echte Subdomain-Aufrufe)
    host = request.headers.get("host", "")
    if not host:
        return None
    
    domain = host.split(":")[0]
    
    # Ignoriere Localhost oder IP-Adressen (Fallback für Dev)
    if "localhost" in domain or "127.0.0.1" in domain:
        # return request.headers.get("x-tenant-id") # Fallback ID
        return "dev"

    parts = domain.split(".")
    if len(parts) >= 3: 
        return parts[0]
    
    return None


async def get_current_tenant(
    request: Request, db: Session = Depends(get_db)
) -> models.Tenant:
    """
    Dependency, die den aktuellen Tenant basierend auf der Subdomain lädt.
    """
    subdomain = get_subdomain(request)
    print(subdomain, 2387764238428)
    if not subdomain:
        # Versuche Fallback ID wenn keine Subdomain da ist
        tenant_id_header = request.headers.get("x-tenant-id")
        if tenant_id_header:
            tenant = db.query(models.Tenant).filter(models.Tenant.id == int(tenant_id_header)).first()
            if tenant: return tenant

        raise HTTPException(status_code=404, detail="No tenant specified (subdomain missing)")

    tenant = crud.get_tenant_by_subdomain(db, subdomain=subdomain)
    print(tenant, 2387764238312313123428)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"School '{subdomain}' not found")
        
    if not tenant.is_active:
        raise HTTPException(status_code=400, detail="School account is inactive")

    return tenant


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
        
        email: str = payload.get("email")
        if not email:
            email = payload.get("sub")
        
        if email is None:
            raise credentials_exception
            
        token_data = schemas.TokenData(email=email)
        
    except JWTError:
        raise credentials_exception

    user = crud.get_user_by_email(db, email=token_data.email, tenant_id=tenant.id)
    
    if user is None:
        raise HTTPException(status_code=401, detail="User not found in this school")

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

def verify_active_subscription(tenant: models.Tenant = Depends(get_current_tenant)):
    """
    Blockiert den Zugriff, wenn das Abo abgelaufen ist.
    Wird für alle Schreib-Operationen (POST, PUT, DELETE) verwendet.
    """
    now = datetime.now(timezone.utc)
    
    # Toleranz: Wir geben evtl. 24h Puffer, damit nicht mitten am Tag abgeschaltet wird
    if tenant.subscription_ends_at and tenant.subscription_ends_at < now:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, # Spezieller Code für Frontend
            detail="Subscription expired. Please update your payment details."
        )
    return tenant