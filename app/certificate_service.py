import io
import logging
from datetime import datetime
from . import models, storage_service, crud

logger = logging.getLogger("pfotencard")

def prepare_certificate_data(
    template: models.CertificateTemplate,
    db=None,
    dog: models.Dog = None,
    user: models.User = None,
    issuer: models.User = None,
    preview_data: dict = None,
    appointment: models.Appointment = None
) -> dict:
    """
    Bereitet die Daten für ein Zertifikat vor, egal ob für reale Erstellung oder Vorschau.
    Priorisiert echte Daten (user/dog/issuer) vor preview_data.
    """
    preview_data = preview_data or {}
    tenant = user.tenant if user and user.tenant else template.tenant
    
    # --- 1. Hundeschule Daten ---
    school_name = preview_data.get("hundeschule_name")
    school_location = preview_data.get("ort") or "Ascha"
    
    if not school_name:
        if tenant:
            school_name = tenant.name
            if tenant.config:
                billing = tenant.config.get("billing_address", {})
                school_location = billing.get("city", school_location)
        else:
            school_name = "Deine Hundeschule"

    # --- 2. Kunden & Hunde Daten ---
    user_name = preview_data.get("kundenname") or "Frau Andrea Lorenz"
    if user:
        if user.vorname and user.nachname:
            user_name = f"{user.vorname} {user.nachname}"
        else:
            user_name = user.name or user.email

    dog_name = preview_data.get("hundename") or "Basco"
    if dog:
        dog_name = dog.name

    # --- 3. Kurs / Level Daten ---
    course_name = preview_data.get("kursname") or template.name or "Musterkurs"
    if db and not preview_data.get("kursname"):
        if template.trigger_type == 'level_achieved':
            level = db.query(models.Level).filter(models.Level.id == template.target_id).first()
            if level:
                course_name = level.name
        elif template.trigger_type == 'course_completed':
            if appointment:
                course_name = appointment.title
            else:
                tt = db.query(models.TrainingType).filter(models.TrainingType.id == template.target_id).first()
                if tt:
                    course_name = tt.name

    # --- 4. Datum & Footer ---
    datum = preview_data.get("datum") or datetime.now().strftime("%d. %B %Y")
    
    sidebar_color = preview_data.get("sidebar_color") or "#8b9370"
    
    footer_text = preview_data.get("footer_text")
    if not footer_text:
        if tenant:
            footer_text = f"www.{tenant.subdomain}.pfotencard.de"
        else:
            footer_text = "www.pfotencard.de"

    # --- 5. Kursleiter & Unterschrift ---
    kursleiter_name = preview_data.get("kursleiter")
    
    # Falls kein Kursleiter übergeben, versuche ihn vom issuer zu bekommen
    if not kursleiter_name and issuer:
        kursleiter_name = f"{issuer.vorname or ''} {issuer.nachname or ''}".strip()
        if not kursleiter_name:
            kursleiter_name = issuer.name or issuer.email
            
    # Fallback wenn immer noch nichts da ist
    if not kursleiter_name:
        kursleiter_name = "Deine Hundeschule"

    saved_signatures = {}
    if tenant and tenant.config:
        saved_signatures = tenant.config.get("signatures", {})
        if not preview_data.get("kursleiter") and not issuer and saved_signatures:
             # Im reinen Vorschau-Modus (ohne User/Issuer) nimm die erste verfügbare Unterschrift
             kursleiter_name = list(saved_signatures.keys())[0]

    # Bilder klonen und dynamische Referenzen auflösen
    variable_values = {
        "kundenname": user_name,
        "hundename": dog_name,
        "datum": datum,
        "hundeschule_name": school_name,
        "kursname": course_name,
        "ort": school_location,
        "kursleiter": kursleiter_name,
        "footer_text": footer_text,
        "sidebar_color": sidebar_color
    }

    final_images = {}
    branding = tenant.config.get("branding", {}) if tenant and tenant.config else {}
    if template.images:
        for slot_id, value in template.images.items():
            if isinstance(value, str) and value.startswith("ref:"):
                var_name = value[4:]
                
                # Spezialfall: Hundeschule Logo aus Branding laden
                if var_name == "hundeschule_name":
                    logo_url = branding.get("logo_url")
                    if logo_url:
                        final_images[slot_id] = logo_url
                        continue

                var_value = variable_values.get(var_name)
                if var_value and var_value in saved_signatures:
                    final_images[slot_id] = saved_signatures[var_value]
                else:
                    # Fallback für Kursleiter-Signaturen: Falls var_value der Name ist, 
                    # aber nicht in signatures, schau ob es eine generische Signatur gibt.
                    logger.debug(f"Variable {var_name}={var_value} not found in signatures.")
            else:
                final_images[slot_id] = value

    # ABWÄRTSKOMPATIBILITÄT: Alte Automatik-Logik für Unterschriften beibehalten
    # (falls noch keine Referenz manuell im Template gesetzt wurde)
    if kursleiter_name in saved_signatures:
        sig_url = saved_signatures[kursleiter_name]
        # In den richtigen Slot packen, je nach Layout (Logik aus Router kopiert)
        if template.layout_id == "layout_professional" and not final_images.get("signature_2"):
            final_images["signature_2"] = sig_url
        elif template.layout_id != "layout_professional" and not final_images.get("signature"):
            final_images["signature"] = sig_url

    # Layout-Metadaten laden, um Trigger-Defaults zu erhalten
    from .certificates.manager import manager
    layout_meta = manager.get_layout_metadata(template.layout_id or "layout_modern")
    
    # Trigger-spezifische Daten aus dem Layout laden
    trigger_defaults = {}
    if layout_meta and template.trigger_type in layout_meta.trigger_data:
        trigger_defaults = layout_meta.trigger_data[template.trigger_type]

    # Priorität für body_text: Preview-Daten > Datenbank > Layout-Trigger-Standard
    default_text = trigger_defaults.get("body_text", "")
    body_text = preview_data.get("body_text") or template.body_text or default_text
    
    # Restliche Variablen vorbereiten
    result_data = {
        "title": template.title,
        "kundenname": user_name,
        "hundename": dog_name,
        "datum": datum,
        "hundeschule_name": school_name,
        "kursname": course_name,
        "ort": school_location,
        "kursleiter": kursleiter_name,
        "sidebar_color": sidebar_color,
        "footer_text": footer_text,
        "images": final_images
    }

    # Trigger-spezifische Overrides aus dem Layout hinzufügen/überschreiben
    # (Dies ermöglicht beliebige neue Variablen im HTML, die im JSON definiert sind)
    for key, val in trigger_defaults.items():
        if key != "body_text": # body_text wurde oben schon speziell behandelt
            result_data[key] = val

    # Platzhalter im body_text ersetzen (Frontend nutzt {kundenname}, wir haben variable_values + result_data)
    all_vars = {**variable_values, **result_data}
    for key, val in all_vars.items():
        placeholder = "{" + key + "}"
        if placeholder in body_text:
            # Wenn es einer der Hauptnamen ist, fügen wir das Styling hinzu
            if key in ['kundenname', 'hundename', 'kursname']:
                styled_val = f'<span class="names {key}">{val}</span>'
                body_text = body_text.replace(placeholder, styled_val)
            else:
                body_text = body_text.replace(placeholder, str(val))
    
    # Zeilenumbrüche in <br> umwandeln für HTML
    body_text = body_text.replace("\n", "<br>")
    result_data["body_text"] = body_text

    return result_data

def generate_certificate_pdf(template: models.CertificateTemplate, db=None, dog: models.Dog = None, user: models.User = None, issuer: models.User = None, appointment: models.Appointment = None) -> io.BytesIO:
    """
    Generiert ein Teilnahmebescheinigungs-PDF basierend auf einer Vorlage.
    """
    from .certificates.manager import manager
    
    layout_id = template.layout_id or 'layout_modern'
    
    # Check if layout exists
    layout = manager.get_layout_metadata(layout_id)
    if not layout:
        logger.error(f"Layout {layout_id} not found. Using default.")
        layout_id = 'layout_modern'

    # Daten vorbereiten
    render_data = prepare_certificate_data(template, db, dog, user, issuer, appointment=appointment)
    
    # PDF mit WeasyPrint (via manager) rendern
    buffer = manager.render_pdf(layout_id, render_data)

    return buffer

def trigger_certificate_generation(db, tenant_id: int, trigger_type: str, target_id: int, user_id: int, dog_id: int = None, issuer_id: int = None, appointment_id: int = None):
    """
    Sucht nach einer passenden Vorlage und generiert das Zertifikat für einen User/Hund.
    """
    logger.info(f"DEBUG: Entering trigger_certificate_generation: tenant_id={tenant_id}, trigger_type={trigger_type}, target_id={target_id}, user_id={user_id}, dog_id={dog_id}, issuer_id={issuer_id}, appointment_id={appointment_id}")
    template = db.query(models.CertificateTemplate).filter(
        models.CertificateTemplate.tenant_id == tenant_id,
        models.CertificateTemplate.trigger_type == trigger_type,
        models.CertificateTemplate.target_id == target_id
    ).first()

    if not template:
        logger.info(f"DEBUG: Keine Zertifikats-Vorlage für {trigger_type} ID {target_id} in Tenant {tenant_id} gefunden.")
        return None

    dog = None
    if dog_id:
        dog = db.query(models.Dog).filter(models.Dog.id == dog_id).first()
        logger.info(f"DEBUG: Found dog: {dog.name if dog else 'None'}")
    
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        logger.warning(f"DEBUG: User mit ID {user_id} nicht gefunden für Zertifikat.")
        return None
        
    issuer = None
    if issuer_id:
        issuer = db.query(models.User).filter(models.User.id == issuer_id).first()

    appointment = None
    if appointment_id:
        appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()

    logger.info(f"DEBUG: Generiere Zertifikat '{template.name}' für User {user.name} ({user_id}) and Hund {dog.name if dog else 'N/A'} ({dog_id})")

    # PDF generieren
    try:
        pdf_buffer = generate_certificate_pdf(template, db, dog, user, issuer, appointment=appointment)
        pdf_content = pdf_buffer.getvalue()
        logger.info(f"DEBUG: PDF generated, size: {len(pdf_content)} bytes")
    except Exception as e:
        logger.error(f"DEBUG: Error during generate_certificate_pdf: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

    # Dateiname sicher machen
    safe_name = template.name.replace(" ", "_").replace("/", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    file_name = f"Zertifikat_{safe_name}_{timestamp}.pdf"
    file_path = f"{tenant_id}/{user.id}/{file_name}"
    
    try:
        logger.info(f"DEBUG: Uploading to storage: {file_path}")
        file_url = storage_service.upload_bytes_to_storage(pdf_content, file_path)
        logger.info(f"DEBUG: Zertifikat hochgeladen: {file_url}")

        # In Dokumente eintragen
        logger.info(f"DEBUG: Creating document entry in DB")
        doc = crud.create_document(
            db=db,
            user_id=user.id,
            tenant_id=tenant_id,
            file_name=file_name,
            file_type="certificate",
            file_path=file_path
        )
        if doc:
            db.flush()
            db.refresh(doc)
        logger.info(f"DEBUG: Document created with ID {doc.id if doc else 'None'}")
        return doc
    except Exception as e:
        logger.error(f"DEBUG: Fehler bei der Zertifikatsgenerierung/Upload: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None
