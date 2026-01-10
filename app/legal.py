from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from . import database, models, schemas, auth

router = APIRouter()

@router.post("/avv/accept")
def accept_avv(
    data: schemas.AVVAccept,
    db: Session = Depends(database.get_db),
    tenant: models.Tenant = Depends(auth.get_current_tenant),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    """
    Dokumentiert, dass der Admin den AVV akzeptiert hat.
    """
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Nur Administratoren können den AVV zeichnen.")
    
    tenant.avv_accepted_at = datetime.now(timezone.utc)
    tenant.avv_accepted_version = data.version
    tenant.avv_accepted_by_user_id = current_user.id
    
    db.commit()
    
    return {"status": "accepted", "at": tenant.avv_accepted_at}
