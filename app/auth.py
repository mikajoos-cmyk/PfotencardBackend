from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from . import crud, schemas
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
        # Default expiration time if not provided
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


async def get_current_active_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> schemas.User:
    """
    Validiert den Supabase JWT Token und holt den Benutzer aus der DB.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # --- DEBUGGING START ---
    print(f"DEBUG: Starte Token-Validierung...")
    # Wir zeigen nur die ersten 5 Zeichen des Secrets, um sicherzugehen, dass es das richtige ist
    safe_secret_preview = settings.SECRET_KEY[:5] if settings.SECRET_KEY else "NONE"
    print(f"DEBUG: Verwendetes SECRET_KEY (Start): {safe_secret_preview}...")
    # --- DEBUGGING END ---

    try:
        # 1. Token dekodieren
        # WICHTIG: verify_aud=False ist nötig, da Supabase "authenticated" als Audience nutzt
        payload = jwt.decode(
            token, 
            settings.SECRET_KEY, 
            algorithms=[settings.ALGORITHM], 
            options={"verify_aud": False} 
        )
        
        # --- DEBUGGING START ---
        print(f"DEBUG: Token erfolgreich dekodiert.")
        # print(f"DEBUG: Token Payload: {payload}") # Vorsicht: Zeigt alle Daten im Log
        # --- DEBUGGING END ---

        # 2. E-Mail aus dem Feld "email" lesen (NICHT "sub")
        email: str = payload.get("email")
        
        if email is None:
            print("DEBUG: FEHLER - Keine E-Mail im Token-Feld 'email' gefunden.")
            raise credentials_exception
            
        print(f"DEBUG: E-Mail aus Token extrahiert: {email}")
        token_data = schemas.TokenData(email=email)
        
    except JWTError as e:
        print(f"DEBUG: JWT Error (Dekodierung fehlgeschlagen): {str(e)}")
        # Häufiger Fehler: Signature verification failed -> Falsches Secret
        raise credentials_exception

    # 3. Benutzer in der Datenbank suchen
    user = crud.get_user_by_email(db, email=token_data.email)
    
    if user is None:
        print(f"DEBUG: FEHLER - User mit E-Mail '{token_data.email}' wurde in der SQL-Datenbank NICHT gefunden.")
        # Falls der Token gültig ist, aber der User fehlt -> 401
        raise credentials_exception

    if not user.is_active:
        print(f"DEBUG: FEHLER - User '{token_data.email}' ist inaktiv.")
        raise HTTPException(status_code=400, detail="Inactive user")

    print(f"DEBUG: Login erfolgreich für User ID: {user.id}")
    return user