import io
import logging
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import requests
from . import models, storage_service, crud

logger = logging.getLogger("pfotencard")

def generate_certificate_pdf(template: models.CertificateTemplate, dog: models.Dog = None, user: models.User = None) -> io.BytesIO:
    """
    Generiert ein Teilnahmebescheinigungs-PDF basierend auf einer Vorlage.
    """
    from .certificates.manager import manager
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    layout_id = template.layout_id or 'layout_modern'
    layout = manager.get_layout(layout_id)
    
    if not layout:
        # Fallback to some basic default layout if not found
        logger.error(f"Layout {layout_id} not found. Using default.")
        layout = manager.get_layout('layout_modern') or next(iter(manager.layouts.values()))

    # Vorbereitete Daten für das Layout
    school_name = "Deine Hundeschule"
    if user and user.tenant:
        school_name = user.tenant.name
    elif template.tenant:
        school_name = template.tenant.name
    
    user_name = "Frau Andrea Lorenz"
    if user:
        user_name = f"{user.vorname} {user.nachname}" if user.vorname and user.nachname else (user.name or user.email)
    
    dog_name = dog.name if dog else "Basco"
    
    render_data = {
        "title": template.title,
        "kundenname": user_name,
        "hundename": dog_name,
        "datum": datetime.now().strftime("%d. %B %Y"),
        "hundeschule_name": school_name,
        "kursname": template.name, # Standardmäßig der Name der Vorlage
        "ort": "Ascha",
        "kursleiter": "Christian Huber",
        "images": template.images or {}
    }
    
    from .certificates.manager import manager
    buffer = manager.render_pdf(layout_id, render_data)

    return buffer

def trigger_certificate_generation(db, tenant_id: int, trigger_type: str, target_id: int, user_id: int, dog_id: int = None):
    """
    Sucht nach einer passenden Vorlage und generiert das Zertifikat für einen User/Hund.
    """
    template = db.query(models.CertificateTemplate).filter(
        models.CertificateTemplate.tenant_id == tenant_id,
        models.CertificateTemplate.trigger_type == trigger_type,
        models.CertificateTemplate.target_id == target_id
    ).first()

    if not template:
        logger.info(f"Keine Zertifikats-Vorlage für {trigger_type} ID {target_id} gefunden.")
        return None

    dog = None
    if dog_id:
        dog = db.query(models.Dog).filter(models.Dog.id == dog_id).first()
    
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        logger.warning(f"User mit ID {user_id} nicht gefunden für Zertifikat.")
        return None

    logger.info(f"Generiere Zertifikat '{template.name}' für User {user_id} und Hund {dog_id}")

    # PDF generieren
    pdf_buffer = generate_certificate_pdf(template, dog, user)
    pdf_content = pdf_buffer.getvalue()

    # In Storage hochladen
    # Dateiname sicher machen
    safe_name = template.name.replace(" ", "_").replace("/", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    file_name = f"Zertifikat_{safe_name}_{timestamp}.pdf"
    file_path = f"{tenant_id}/{user.id}/{file_name}"
    
    try:
        file_url = storage_service.upload_bytes_to_storage(pdf_content, file_path)
        logger.info(f"Zertifikat hochgeladen: {file_url}")

        # In Dokumente eintragen
        doc = crud.create_document(
            db=db,
            user_id=user.id,
            tenant_id=tenant_id,
            file_name=file_name,
            file_type="certificate",
            file_path=file_path
        )
        return doc
    except Exception as e:
        logger.error(f"Fehler bei der Zertifikatsgenerierung/Upload: {e}")
        return None
