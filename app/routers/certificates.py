from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse, HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from typing import List, Dict, Any
import io
import logging
from .. import crud, models, schemas, auth, certificate_service
from ..database import get_db

logger = logging.getLogger("pfotencard")

router = APIRouter(prefix="/api/certificates", tags=["certificates"])

@router.get("/layouts", response_model=List[schemas.CertificateLayoutMetadata])
def get_layouts(
    current_user: models.User = Depends(auth.get_current_active_user)
):
    from ..certificates.manager import manager
    layouts = manager.list_layouts()
    return [
        schemas.CertificateLayoutMetadata(
            id=layout.id,
            name=layout.name,
            image_slots=layout.image_slots,
            placeholders=layout.placeholders,
            trigger_data=layout.trigger_data
        ) for layout in layouts
    ]

@router.post("/preview-html")
def preview_html_certificate(
    template_in: schemas.CertificateTemplateCreate,
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    
    from ..certificates.manager import manager
    
    # Dummy Template Objekt erstellen
    template = models.CertificateTemplate(
        name=template_in.name,
        title=template_in.title,
        body_text=template_in.body_text,
        layout_id=template_in.layout_id,
        images=template_in.images,
        trigger_type=template_in.trigger_type,
        target_id=template_in.target_id,
        tenant=current_user.tenant
    )
    
    data = certificate_service.prepare_certificate_data(template, preview_data=template_in.preview_data)
    html_content = manager.render_html(template.layout_id, data)
    
    return HTMLResponse(content=html_content)

@router.post("/preview-sample")
def preview_sample_certificate(
    template_in: schemas.CertificateTemplateCreate,
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    
    # Dummy Template Objekt erstellen
    template = models.CertificateTemplate(
        name=template_in.name,
        title=template_in.title,
        body_text=template_in.body_text,
        layout_id=template_in.layout_id,
        images=template_in.images,
        trigger_type=template_in.trigger_type,
        target_id=template_in.target_id,
        tenant=current_user.tenant
    )
    
    from ..certificates.manager import manager
    data = certificate_service.prepare_certificate_data(template, preview_data=template_in.preview_data)
    pdf_buffer = manager.render_pdf(template.layout_id, data)
    
    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=certificate_preview.pdf"}
    )

@router.post("/templates", response_model=schemas.CertificateTemplateResponse)
def create_template(
    template_in: schemas.CertificateTemplateCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    return crud.create_certificate_template(db, current_user.tenant_id, template_in)

@router.get("/templates", response_model=List[schemas.CertificateTemplateResponse])
def get_templates(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    return crud.get_certificate_templates(db, current_user.tenant_id)

@router.put("/templates/{template_id}", response_model=schemas.CertificateTemplateResponse)
def update_template(
    template_id: int,
    template_in: schemas.CertificateTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    
    template = crud.get_certificate_template(db, template_id)
    if not template or template.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Template nicht gefunden")
        
    return crud.update_certificate_template(db, template_id, template_in)

@router.delete("/templates/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user)
):
    if current_user.role not in ['admin', 'mitarbeiter']:
        raise HTTPException(status_code=403, detail="Nicht berechtigt")
    
    template = crud.get_certificate_template(db, template_id)
    if not template or template.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Template nicht gefunden")
        
    crud.delete_certificate_template(db, template_id)
    return {"ok": True}

@router.get("/employees")
def get_employees(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_active_user)):
    """Holt alle Mitarbeiter dieses Mandanten für die Unterschriften-Zuordnung"""
    STAFF_ROLES = ['admin', 'mitarbeiter', 'staff', 'trainer']
    users = db.query(models.User).filter(
        models.User.tenant_id == current_user.tenant_id,
        models.User.role.in_(STAFF_ROLES)
    ).all()
    # Erstelle den vollen Namen, falle zurück auf Email, falls kein Name gesetzt ist
    result = []
    for u in users:
        name = f"{u.vorname or ''} {u.nachname or ''}".strip()
        if not name:
            name = u.name or u.email
        result.append({"id": u.id, "name": name})
    return result

@router.get("/signatures")
def get_signatures(current_user: models.User = Depends(auth.get_current_active_user)):
    """Holt die gespeicherten Unterschriften-URLs aus der Tenant Config"""
    if current_user.tenant and current_user.tenant.config:
        return current_user.tenant.config.get("signatures", {})
    return {}

@router.put("/signatures")
def save_signatures(signatures: dict, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_active_user)):
    """Speichert die Unterschriften-URLs in der Tenant Config"""
    if current_user.tenant.config is None:
        current_user.tenant.config = {}
    
    current_user.tenant.config["signatures"] = signatures
    flag_modified(current_user.tenant, "config") # Zwingt die DB, das JSON Update zu erkennen
    db.commit()
    return {"status": "ok", "signatures": signatures}
