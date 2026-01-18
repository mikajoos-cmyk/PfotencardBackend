from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import and_, func, or_, case
from datetime import datetime, timedelta, timezone # <--- UPDATE
from . import models, schemas, storage_service
from fastapi import HTTPException
import secrets
from typing import List, Optional

# Notification Service importieren
from .notification_service import notify_user

# --- HELPER ---
def format_datetime_de(dt: datetime) -> str:
    """Hilfsfunktion für deutsche Datumsformatierung"""
    if not dt: return ""
    return dt.strftime("%d.%m.%Y um %H:%M Uhr")

# --- TENANT & CONFIGURATION ---

def get_tenant_by_subdomain(db: Session, subdomain: str):
    return db.query(models.Tenant).filter(models.Tenant.subdomain == subdomain).first()

def delete_tenant(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant: return None
    
    # 1. Physical Storage Cleanup
    storage_service.delete_tenant_storage(tenant_id)
    
    # 2. Database Delete (Cascades through ON DELETE CASCADE)
    db.delete(tenant)
    db.commit()
    return {"ok": True}

def get_app_config(db: Session, tenant_id: int) -> schemas.AppConfig:
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
        
    levels = db.query(models.Level).options(
        joinedload(models.Level.requirements).joinedload(models.LevelRequirement.training_type)
    ).filter(models.Level.tenant_id == tenant_id).order_by(models.Level.rank_order).all()
    
    training_types = db.query(models.TrainingType).filter(
        models.TrainingType.tenant_id == tenant_id
    ).order_by(models.TrainingType.rank_order.asc()).all()
    
    appointments = db.query(models.Appointment).filter(
        models.Appointment.tenant_id == tenant_id
    ).order_by(models.Appointment.start_time.desc()).all()
    
    return schemas.AppConfig(
        tenant=tenant,
        levels=levels,
        training_types=training_types,
        appointments=appointments
    )

def update_tenant_settings(db: Session, tenant_id: int, settings: schemas.SettingsUpdate):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant: raise HTTPException(404, "Tenant not found")

    # 1. Update Tenant Basic Info & Config
    tenant.name = settings.school_name
    tenant.support_email = settings.support_email
    
    # Sicherstellen, dass config ein Dict ist
    current_config = dict(tenant.config) if tenant.config else {}
    
    # Branding Updates
    current_config["branding"] = current_config.get("branding", {})
    current_config["branding"]["primary_color"] = settings.primary_color
    current_config["branding"]["secondary_color"] = settings.secondary_color
    current_config["branding"]["background_color"] = settings.background_color
    current_config["branding"]["sidebar_color"] = settings.sidebar_color
    if settings.logo_url:
        current_config["branding"]["logo_url"] = settings.logo_url
    if settings.open_for_all_color:
        current_config["branding"]["open_for_all_color"] = settings.open_for_all_color
    
    # Wording Updates
    current_config["wording"] = current_config.get("wording", {})
    current_config["wording"]["level"] = settings.level_term
    current_config["wording"]["vip"] = settings.vip_term

    # Balance Updates
    current_config["balance"] = current_config.get("balance", {})
    current_config["balance"]["allow_custom_top_up"] = settings.allow_custom_top_up
    current_config["balance"]["top_up_options"] = [opt.model_dump() for opt in settings.top_up_options]
    
    # Modules Update
    current_config["active_modules"] = settings.active_modules
    
    # NEU: Automatisierungseinstellungen
    current_config["auto_billing_enabled"] = settings.auto_billing_enabled
    current_config["auto_progress_enabled"] = settings.auto_progress_enabled

    tenant.config = current_config
    flag_modified(tenant, "config")
    
    # 2. Sync Services (TrainingTypes)
    existing_services = db.query(models.TrainingType).filter(models.TrainingType.tenant_id == tenant_id).all()
    existing_service_ids = {s.id for s in existing_services}
    payload_service_ids = {s.id for s in settings.services if s.id is not None and s.id > 0}
    
    to_delete_ids = existing_service_ids - payload_service_ids
    if to_delete_ids:
        db.query(models.TrainingType).filter(models.TrainingType.id.in_(to_delete_ids)).delete(synchronize_session=False)
    
    temp_id_mapping = {}

    for s_data in settings.services:
        svc = None
        if s_data.id and s_data.id > 0:
            svc = next((s for s in existing_services if s.id == s_data.id), None)
            
        if svc:
            svc.name = s_data.name
            svc.category = s_data.category
            svc.default_price = s_data.price
            svc.rank_order = s_data.rank_order
        else:
            new_svc = models.TrainingType(
                tenant_id=tenant_id,
                name=s_data.name,
                category=s_data.category,
                default_price=s_data.price,
                rank_order=s_data.rank_order
            )
            db.add(new_svc)
            db.flush()
            if s_data.id and s_data.id < 0:
                temp_id_mapping[s_data.id] = new_svc.id
    
    db.flush()

    # 3. Sync Levels
    existing_levels = db.query(models.Level).filter(models.Level.tenant_id == tenant_id).all()
    existing_level_ids = {l.id for l in existing_levels}
    payload_level_ids = {l.id for l in settings.levels if l.id is not None and l.id > 0}
    
    to_delete_level_ids = existing_level_ids - payload_level_ids
    if to_delete_level_ids:
        db.query(models.Level).filter(models.Level.id.in_(to_delete_level_ids)).delete(synchronize_session=False)
        
    for l_data in settings.levels:
        current_level = None
        if l_data.id and l_data.id > 0:
            current_level = next((l for l in existing_levels if l.id == l_data.id), None)
            if current_level:
                current_level.name = l_data.name
                current_level.rank_order = l_data.rank_order
                current_level.icon_url = l_data.badge_image
                current_level.color = l_data.color
                current_level.has_additional_requirements = l_data.has_additional_requirements
        else:
            current_level = models.Level(
                tenant_id=tenant_id,
                name=l_data.name,
                rank_order=l_data.rank_order,
                icon_url=l_data.badge_image,
                color=l_data.color,
                has_additional_requirements=l_data.has_additional_requirements
            )
            db.add(current_level)
            db.flush()
            
        if current_level.id:
            db.query(models.LevelRequirement).filter(models.LevelRequirement.level_id == current_level.id).delete()
            for req_data in l_data.requirements:
                training_id = req_data.training_type_id
                if training_id in temp_id_mapping:
                    training_id = temp_id_mapping[training_id]

                new_req = models.LevelRequirement(
                    level_id=current_level.id,
                    training_type_id=training_id,
                    required_count=req_data.required_count,
                    is_additional=req_data.is_additional
                )
                db.add(new_req)

    db.commit()
    db.refresh(tenant)
    return tenant

# --- USER ---

def get_user(db: Session, user_id: int, tenant_id: int):
    return db.query(models.User).options(
        joinedload(models.User.documents),
        joinedload(models.User.achievements),
        joinedload(models.User.dogs),
        joinedload(models.User.current_level)
    ).filter(
        models.User.id == user_id, 
        models.User.tenant_id == tenant_id
    ).first()

def get_user_by_auth_id(db: Session, auth_id: str, tenant_id: int):
    return db.query(models.User).options(
        joinedload(models.User.documents),
        joinedload(models.User.achievements),
        joinedload(models.User.dogs),
        joinedload(models.User.current_level)
    ).filter(
        models.User.auth_id == auth_id,
        models.User.tenant_id == tenant_id
    ).first()

def get_user_by_email(db: Session, email: str, tenant_id: int):
    return db.query(models.User).filter(
        models.User.email == email, 
        models.User.tenant_id == tenant_id
    ).first()

def get_users(db: Session, tenant_id: int, skip: int = 0, limit: int = 100, portfolio_of_user_id: Optional[int] = None):
    query = db.query(models.User).options(
        joinedload(models.User.documents),
        joinedload(models.User.achievements),
        joinedload(models.User.dogs),
        joinedload(models.User.current_level)
    ).filter(models.User.tenant_id == tenant_id)
    
    if portfolio_of_user_id:
        customer_ids = db.query(models.Transaction.user_id).filter(
            models.Transaction.booked_by_id == portfolio_of_user_id,
            models.Transaction.tenant_id == tenant_id
        ).distinct()
        query = query.filter(models.User.id.in_(customer_ids))

    return query.order_by(models.User.name).offset(skip).limit(limit).all()

def search_users(db: Session, tenant_id: int, search_term: str):
    return db.query(models.User).filter(
        models.User.tenant_id == tenant_id,
        models.User.name.ilike(f"%{search_term}%")
    ).all()

def create_user(db: Session, user: schemas.UserCreate, tenant_id: int, auth_id: Optional[str] = None):
    from . import auth
    if not user.password and not auth_id:
        user.password = secrets.token_urlsafe(16)

    hashed_password = auth.get_password_hash(user.password) if user.password else None
    
    start_level = db.query(models.Level).filter(
        models.Level.tenant_id == tenant_id,
        models.Level.rank_order == 1
    ).first()
    
    db_user = models.User(
        tenant_id=tenant_id,
        auth_id=auth_id,
        email=user.email,
        name=user.name,
        role=user.role,
        is_active=user.is_active,
        balance=user.balance,
        phone=user.phone,
        current_level_id=start_level.id if start_level else None,
        hashed_password=hashed_password
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    for dog_data in user.dogs:
        create_dog_for_user(db, dog_data, db_user.id, tenant_id)
        
    return db_user

def update_user(db: Session, user_id: int, tenant_id: int, user: schemas.UserUpdate):
    from . import auth  
    db_user = get_user(db, user_id, tenant_id)
    if not db_user:
        return None

    update_data = user.model_dump(exclude_unset=True)
    if "password" in update_data and update_data["password"]:
        db_user.hashed_password = auth.get_password_hash(update_data.pop("password"))

    for key, value in update_data.items():
        if key == "level_id":
             db_user.current_level_id = value
        else:
             setattr(db_user, key, value)

    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def update_user_status(db: Session, user_id: int, tenant_id: int, status: schemas.UserStatusUpdate):
    db_user = get_user(db, user_id, tenant_id)
    if not db_user: return None

    update_data = status.model_dump(exclude_unset=True)
    
    if update_data.get("is_vip") is True:
        db_user.is_expert = False
    elif update_data.get("is_expert") is True:
        db_user.is_vip = False

    for key, value in update_data.items():
        setattr(db_user, key, value)

    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def update_user_level(db: Session, user_id: int, new_level_id: int):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user: return None
    user.current_level_id = new_level_id
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

def delete_user(db: Session, user_id: int, tenant_id: int):
    db_user = get_user(db, user_id, tenant_id)
    if not db_user: return None
    
    # 0. Manual Cleanup for references (DB constraints might not have ON DELETE CASCADE correctly set up)
    # A. Chat Messages (Sender OR Receiver)
    db.query(models.ChatMessage).filter(
        or_(models.ChatMessage.sender_id == user_id, models.ChatMessage.receiver_id == user_id)
    ).delete(synchronize_session=False)
    
    # B. Bookings
    db.query(models.Booking).filter(models.Booking.user_id == user_id).delete(synchronize_session=False)

    # C. Dogs
    db.query(models.Dog).filter(models.Dog.owner_id == user_id).delete(synchronize_session=False)

    # D. Transactions (Own transactions)
    db.query(models.Transaction).filter(models.Transaction.user_id == user_id).delete(synchronize_session=False)
    
    # E. Transactions booked_by (SET NULL) - Important for staff deletion
    db.query(models.Transaction).filter(models.Transaction.booked_by_id == user_id).update(
        {models.Transaction.booked_by_id: None}, synchronize_session=False
    )
    
    # F. Appointment trainer (SET NULL)
    db.query(models.Appointment).filter(models.Appointment.trainer_id == user_id).update(
        {models.Appointment.trainer_id: None}, synchronize_session=False
    )
    
    # G. News posts created_by (Delete)
    db.query(models.NewsPost).filter(models.NewsPost.created_by_id == user_id).delete(synchronize_session=False)

    # H. Achievements
    db.query(models.Achievement).filter(models.Achievement.user_id == user_id).delete(synchronize_session=False)

    # I. Documents
    db.query(models.Document).filter(models.Document.user_id == user_id).delete(synchronize_session=False)

    # J. Push Subscriptions
    db.query(models.PushSubscription).filter(models.PushSubscription.user_id == user_id).delete(synchronize_session=False)

    # 1. Physical Storage Cleanup
    storage_service.delete_user_storage(tenant_id, user_id)
    
    # 2. Database Delete the User itself
    db.delete(db_user)
    db.commit()
    return {"ok": True}

# --- DOGS ---

def get_dog(db: Session, dog_id: int, tenant_id: int):
    return db.query(models.Dog).filter(
        models.Dog.id == dog_id,
        models.Dog.tenant_id == tenant_id
    ).first()

def create_dog_for_user(db: Session, dog: schemas.DogCreate, user_id: int, tenant_id: int):
    db_dog = models.Dog(**dog.model_dump(), owner_id=user_id, tenant_id=tenant_id)
    db.add(db_dog)
    db.commit()
    db.refresh(db_dog)
    return db_dog

def update_dog(db: Session, dog_id: int, tenant_id: int, dog: schemas.DogBase):
    db_dog = get_dog(db, dog_id, tenant_id)
    if not db_dog: return None
    
    for key, value in dog.model_dump(exclude_unset=True).items():
        setattr(db_dog, key, value)
        
    db.add(db_dog)
    db.commit()
    db.refresh(db_dog)
    return db_dog

def delete_dog(db: Session, dog_id: int, tenant_id: int):
    db_dog = get_dog(db, dog_id, tenant_id)
    if not db_dog:
        return None
        
    image_path_to_delete = db_dog.image_url # Falls du die URL/Pfad speicherst
    
    db.delete(db_dog)
    db.commit()
    
    return {"ok": True, "image_path": image_path_to_delete}


# --- LEVEL & ACHIEVEMENTS LOGIC (DYNAMISCH) ---

def check_level_up_eligibility(db: Session, user: models.User) -> bool:
    if not user.current_level_id:
        return False

    current_level = db.query(models.Level).filter(models.Level.id == user.current_level_id).first()
    next_level = db.query(models.Level).filter(
        models.Level.tenant_id == user.tenant_id,
        models.Level.rank_order > current_level.rank_order
    ).order_by(models.Level.rank_order.asc()).first()

    if not next_level:
        return False

    requirements = db.query(models.LevelRequirement).options(
        joinedload(models.LevelRequirement.training_type)
    ).filter(
        models.LevelRequirement.level_id == current_level.id,
        models.LevelRequirement.is_additional == False
    ).all()

    if not requirements:
        return True

    # Split requirements into exam and non-exam
    exam_reqs = [r for r in requirements if r.training_type and r.training_type.category == 'exam']
    non_exam_reqs = [r for r in requirements if not (r.training_type and r.training_type.category == 'exam')]

    unconsumed_achievements = db.query(
        models.Achievement.training_type_id, 
        func.count(models.Achievement.id)
    ).filter(
        models.Achievement.user_id == user.id,
        models.Achievement.is_consumed == False,
        models.Achievement.tenant_id == user.tenant_id
    ).group_by(models.Achievement.training_type_id).all()
    
    achievement_map = {TypeId: Count for TypeId, Count in unconsumed_achievements}

    # 1. Check non-exam requirements first
    for req in non_exam_reqs:
        available = achievement_map.get(req.training_type_id, 0)
        if available < req.required_count:
            return False

    # 2. Only if all non-exam requirements are met, check exams
    for req in exam_reqs:
        available = achievement_map.get(req.training_type_id, 0)
        if available < req.required_count:
            return False

    return True


def are_non_exam_requirements_met(db: Session, user: models.User, current_level: models.Level = None) -> bool:
    if not current_level:
        if not user.current_level_id:
            return False
        current_level = db.query(models.Level).filter(models.Level.id == user.current_level_id).first()
        if not current_level: return False

    requirements = db.query(models.LevelRequirement).options(
        joinedload(models.LevelRequirement.training_type)
    ).filter(
        models.LevelRequirement.level_id == current_level.id,
        models.LevelRequirement.is_additional == False
    ).all()

    if not requirements:
        return True

    # Check non-exam requirements
    non_exam_reqs = [r for r in requirements if not (r.training_type and r.training_type.category == 'exam')]
    if not non_exam_reqs:
        return True

    unconsumed_achievements = db.query(
        models.Achievement.training_type_id, 
        func.count(models.Achievement.id)
    ).filter(
        models.Achievement.user_id == user.id,
        models.Achievement.is_consumed == False,
        models.Achievement.tenant_id == user.tenant_id
    ).group_by(models.Achievement.training_type_id).all()
    
    achievement_map = {TypeId: Count for TypeId, Count in unconsumed_achievements}

    for req in non_exam_reqs:
        available = achievement_map.get(req.training_type_id, 0)
        if available < req.required_count:
            return False
            
    return True

def perform_level_up(db: Session, user_id: int, tenant_id: int):
    user = get_user(db, user_id, tenant_id)
    if not user: raise HTTPException(404, "User not found")
    
    if not check_level_up_eligibility(db, user):
        raise HTTPException(400, "Requirements not met")

    current_level = db.query(models.Level).filter(models.Level.id == user.current_level_id).first()
    requirements = db.query(models.LevelRequirement).filter(
        models.LevelRequirement.level_id == current_level.id,
        models.LevelRequirement.is_additional == False
    ).all()

    for req in requirements:
        # Mark ALL unconsumed achievements of this type as consumed, even if they exceed the required count
        achievements_to_consume = db.query(models.Achievement).filter(
            models.Achievement.user_id == user.id,
            models.Achievement.tenant_id == tenant_id,
            models.Achievement.training_type_id == req.training_type_id,
            models.Achievement.is_consumed == False
        ).all()
        
        for ach in achievements_to_consume:
            ach.is_consumed = True
            db.add(ach)

    next_level = db.query(models.Level).filter(
        models.Level.tenant_id == tenant_id,
        models.Level.rank_order > current_level.rank_order
    ).order_by(models.Level.rank_order.asc()).first()
    
    user.current_level_id = next_level.id
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

# --- TRANSACTIONS & ACHIEVEMENTS ---

def create_transaction(db: Session, transaction: schemas.TransactionCreate, booked_by_id: Optional[int], tenant_id: int):
    user = get_user(db, transaction.user_id, tenant_id)
    if not user: raise HTTPException(404, "User not found")

    # SAFETY FIX: Ensure 0 is treated as None for the DB foreign key
    if not booked_by_id or booked_by_id == 0:
        booked_by_id = user.id

    amount_to_add = transaction.amount
    bonus = 0
    
    # DYNAMISCHE BONUS-BERECHNUNG aus Tenant Config
    if transaction.type == "Aufladung":
        tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
        if tenant and tenant.config and "balance" in tenant.config:
            top_up_options = tenant.config["balance"].get("top_up_options", [])
            # Sortiere absteigend, um den höchsten zutreffenden Bonus zu finden
            # (Annahme: Optionen sind [{"amount": 300, "bonus": 150}, ...])
            sorted_options = sorted(top_up_options, key=lambda x: x.get("amount", 0), reverse=True)
            
            for option in sorted_options:
                threshold = option.get("amount", 0)
                bonus_val = option.get("bonus", 0)
                if amount_to_add >= threshold:
                    bonus = bonus_val
                    break

    total_change = amount_to_add + bonus
    user.balance += total_change
    db.add(user)

    db_tx = models.Transaction(
        tenant_id=tenant_id,
        user_id=user.id,
        booked_by_id=booked_by_id,
        type=transaction.type,
        description=transaction.description,
        amount=total_change, # Gesamtbetrag auf dem Konto
        balance_after=user.balance,
        bonus=bonus # NEU: Hier wird der Bonus festgeschrieben!
    )
    db.add(db_tx)
    db.flush()

    if transaction.training_type_id:
        tt = db.query(models.TrainingType).filter(
            models.TrainingType.id == transaction.training_type_id,
            models.TrainingType.tenant_id == tenant_id
        ).first()
        
        if tt:
            create_achievement(db, user.id, tenant_id, tt.id, db_tx.id)

    db.commit()
    db.refresh(db_tx)
    return db_tx


def create_achievement(db: Session, user_id: int, tenant_id: int, training_type_id: int, transaction_id: Optional[int] = None, date_achieved: Optional[datetime] = None):
    ach = models.Achievement(
        tenant_id=tenant_id,
        user_id=user_id,
        training_type_id=training_type_id,
        transaction_id=transaction_id,
        date_achieved=date_achieved or datetime.now(timezone.utc)
    )

    # CHECK: Premature Exam?
    # check if 'exam' category
    tt = db.query(models.TrainingType).filter(models.TrainingType.id == training_type_id).first()
    if tt and tt.category == 'exam':
        user = get_user(db, user_id, tenant_id)
        if user:
             if not are_non_exam_requirements_met(db, user):
                 # Premature exam! Mark as consumed so it doesn't count.
                 ach.is_consumed = True

    db.add(ach)
    return ach

# Update: Filter für user_id hinzugefügt, um spezifische Kundenhistorien zu laden
def get_transactions_for_user(db: Session, user_id: int, tenant_id: int, for_staff: bool = False, specific_customer_id: Optional[int] = None):
    query = db.query(models.Transaction).filter(models.Transaction.tenant_id == tenant_id)
    
    if for_staff:
        # Mitarbeiter sieht normalerweise seine Buchungen...
        if specific_customer_id:
             # ... aber wenn er einen Kunden öffnet, sieht er dessen Historie
             query = query.filter(models.Transaction.user_id == specific_customer_id)
        else:
             query = query.filter(models.Transaction.booked_by_id == user_id)
    else:
        # Kunden sehen immer nur ihre eigenen
        query = query.filter(models.Transaction.user_id == user_id)
        
    return query.order_by(models.Transaction.date.desc()).all()

# --- DOCUMENTS ---

def create_document(db: Session, user_id: int, tenant_id: int, file_name: str, file_type: str, file_path: str):
    doc = models.Document(
        tenant_id=tenant_id,
        user_id=user_id,
        file_name=file_name,
        file_type=file_type,
        file_path=file_path
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    
    # --- NEU: User benachrichtigen ---
    notify_user(
        db=db,
        user_id=user_id,
        type="system", 
        title="Neues Dokument",
        message="Ein neues Dokument wurde in deiner Akte hinterlegt.",
        url="/profile",
        details={
            "Dokument": file_name,
            "Datum": format_datetime_de(doc.upload_date)
        }
    )
    
    return doc

def get_document(db: Session, document_id: int, tenant_id: int):
    return db.query(models.Document).filter(
        models.Document.id == document_id,
        models.Document.tenant_id == tenant_id
    ).first()

def delete_document(db: Session, document_id: int, tenant_id: int):
    # Erst das Dokument holen, um den Dateipfad zu kennen
    db_document = db.query(models.Document).filter(
        models.Document.id == document_id,
        models.Document.tenant_id == tenant_id
    ).first()
    
    if not db_document:
        return None

    file_path_to_delete = db_document.file_path  # Speichere Pfad vor dem Löschen
    
    db.delete(db_document)
    db.commit()
    
    # Gib den Pfad zurück, damit der Controller weiß, was er im Storage löschen muss
    return {"ok": True, "file_path": file_path_to_delete}



# --- APPOINTMENTS & BOOKINGS ---

def create_appointment(db: Session, appointment: schemas.AppointmentCreate, tenant_id: int):
    print(f"DEBUG: Creating appointment with trainer_id={appointment.trainer_id}, target_levels={appointment.target_level_ids}, training_type_id={appointment.training_type_id}")
    db_appt = models.Appointment(
        tenant_id=tenant_id,
        title=appointment.title,
        description=appointment.description,
        start_time=appointment.start_time,
        end_time=appointment.end_time,
        location=appointment.location,
        max_participants=appointment.max_participants,
        trainer_id=appointment.trainer_id,
        training_type_id=appointment.training_type_id,
        is_open_for_all=appointment.is_open_for_all
    )
    
    if appointment.target_level_ids:
        levels = db.query(models.Level).filter(models.Level.id.in_(appointment.target_level_ids)).all()
        db_appt.target_levels = levels

    db.add(db_appt)
    db.commit()
    db.refresh(db_appt)
    return db_appt

def get_appointments(db: Session, tenant_id: int):
    results = db.query(
        models.Appointment,
        func.count(models.Booking.id).label('count')
    ).outerjoin(
        models.Booking,
        and_(
            models.Booking.appointment_id == models.Appointment.id,
            models.Booking.status == 'confirmed'
        )
    ).filter(
        models.Appointment.tenant_id == tenant_id
    ).options(
        joinedload(models.Appointment.trainer),
        joinedload(models.Appointment.target_levels)
    ).group_by(
        models.Appointment.id
    ).order_by(
        models.Appointment.start_time.asc()
    ).all()
    
    appointments = []
    for appt, count in results:
        appt.participants_count = count
        appointments.append(appt)
        
    return appointments

def get_appointment(db: Session, appointment_id: int, tenant_id: int):
    return db.query(models.Appointment).options(
        joinedload(models.Appointment.target_levels),
        joinedload(models.Appointment.trainer)
    ).filter(
        models.Appointment.id == appointment_id,
        models.Appointment.tenant_id == tenant_id
    ).first()

def update_appointment(db: Session, appointment_id: int, tenant_id: int, update: schemas.AppointmentUpdate):
    print(f"DEBUG: Updating appointment {appointment_id} with data={update.model_dump()}")
    db_appt = get_appointment(db, appointment_id, tenant_id)
    if not db_appt:
        return None

    update_data = update.model_dump(exclude_unset=True)
    
    if "target_level_ids" in update_data:
        target_level_ids = update_data.pop("target_level_ids")
        levels = db.query(models.Level).filter(models.Level.id.in_(target_level_ids)).all()
        db_appt.target_levels = levels

    for key, value in update_data.items():
        setattr(db_appt, key, value)

    db.add(db_appt)
    db.commit()
    db.refresh(db_appt)
    return db_appt

def delete_appointment(db: Session, appointment_id: int, tenant_id: int):
    db_appt = get_appointment(db, appointment_id, tenant_id)
    if not db_appt:
        return False
    
    # --- NEU: Teilnehmer benachrichtigen ---
    participants = db.query(models.Booking).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.status == 'confirmed'
    ).all()
    
    formatted_date = format_datetime_de(db_appt.start_time)
    
    for p in participants:
        notify_user(
            db=db,
            user_id=p.user_id,
            type="alert",
            title="Termin abgesagt",
            message=f"Der Termin '{db_appt.title}' am {formatted_date} wurde leider abgesagt.",
            url="/bookings",
            details={
                "Kurs": db_appt.title,
                "Ursprünglicher Termin": formatted_date,
                "Grund": "Terminabsage durch Hundeschule"
            }
        )

    db.delete(db_appt)
    db.commit()
    return True

def create_booking(db: Session, tenant_id: int, appointment_id: int, user_id: int):
    appt = get_appointment(db, appointment_id, tenant_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")
        
    existing = db.query(models.Booking).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.user_id == user_id
    ).first()
    
    if existing:
        if existing.status == 'cancelled':
            # Re-Aktivierung: Prüfen ob Platz frei ist
            current_count = db.query(models.Booking).filter(
                models.Booking.appointment_id == appointment_id,
                models.Booking.status == 'confirmed'
            ).count()
            
            if current_count >= appt.max_participants:
                existing.status = 'waitlist'
            else:
                existing.status = 'confirmed'
                
            db.commit()
            db.refresh(existing)
            notify_user(
                db=db,
                user_id=user_id,
                type="booking",
                title="Buchung reaktiviert",
                message=f"Deine Buchung für '{appt.title}' ist wieder aktiv ({existing.status}).",
                url="/bookings",
                details={
                    "Kurs": appt.title,
                    "Datum": format_datetime_de(appt.start_time),
                    "Status": "Warteliste" if existing.status == 'waitlist' else "Bestätigt"
                }
            )
            return existing
        else:
            raise HTTPException(400, "Already booked or on waitlist")

    # Zähle NUR bestätigte Buchungen
    current_count = db.query(models.Booking).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.status == 'confirmed'
    ).count()
    
    # Entscheidung: Warteliste oder Bestätigt
    new_status = 'confirmed'
    if current_count >= appt.max_participants:
        new_status = 'waitlist'

    booking = models.Booking()
    new_booking = models.Booking(
        tenant_id=tenant_id,
        appointment_id=appointment_id,
        user_id=user_id,
        status=new_status
    )
    db.add(new_booking)
    db.commit()
    db.refresh(new_booking)
    
    # Benachrichtigung senden
    notify_user(
        db=db,
        user_id=user_id,
        type="booking",
        title=f"Termin {'bestätigt' if new_status == 'confirmed' else 'auf Warteliste'}",
        message=f"Du hast dich erfolgreich für '{appt.title}' angemeldet.",
        url="/bookings",
        details={
            "Kurs": appt.title,
            "Datum": format_datetime_de(appt.start_time),
            "Ort": appt.location or "Wie angegeben",
            "Status": "Warteliste" if new_status == 'waitlist' else "Teilnahme bestätigt"
        }
    )
    
    return new_booking

def cancel_booking(db: Session, tenant_id: int, appointment_id: int, user_id: int):
    booking = db.query(models.Booking).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.user_id == user_id,
        models.Booking.tenant_id == tenant_id
    ).first()
    
    if not booking:
        raise HTTPException(404, "Booking not found")
    
    previous_status = booking.status
    booking.status = 'cancelled'
    # Wichtig: Wenn jemand absagt, Reset der Anwesenheit
    booking.attended = False 
    
    promoted_user_id = None
    
    # AUTOMATISCHES NACHRÜCKEN
    # Nur wenn der stornierte Platz 'confirmed' war, rückt jemand nach
    if previous_status == 'confirmed':
        # Finde den ältesten Eintrag auf der Warteliste
        next_in_line = db.query(models.Booking).filter(
            models.Booking.appointment_id == appointment_id,
            models.Booking.tenant_id == tenant_id,
            models.Booking.status == 'waitlist'
        ).order_by(models.Booking.created_at.asc()).first()
        
        if next_in_line:
            next_in_line.status = 'confirmed'
            promoted_user_id = next_in_line.user_id
            
            # --- NEU: Nachrücker benachrichtigen ---
            notify_user(
                db=db,
                user_id=promoted_user_id,
                type="booking",
                title="Platz bestätigt (Nachgerückt)",
                message=f"Gute Nachrichten! Du bist für '{booking.appointment.title}' nachgerückt.",
                url="/bookings",
                details={
                    "Kurs": booking.appointment.title,
                    "Datum": format_datetime_de(booking.appointment.start_time),
                    "Hinweis": "Du warst auf der Warteliste und hast nun einen Platz."
                }
            )

    # User über Storno informieren
    notify_user(
        db=db,
        user_id=user_id,
        type="booking",
        title="Buchung storniert",
        message=f"Du hast deine Teilnahme an '{booking.appointment.title}' storniert.",
        url="/bookings",
        details={
            "Kurs": booking.appointment.title,
            "Datum": format_datetime_de(booking.appointment.start_time)
        }
    )

    db.commit()
    
    return {
        "ok": True, 
        "status": "cancelled", 
        "promoted_user_id": promoted_user_id
    }

def get_participants(db: Session, tenant_id: int, appointment_id: int):
    return db.query(models.Booking).options(joinedload(models.Booking.user)).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.tenant_id == tenant_id
    ).all()

def get_user_bookings(db: Session, tenant_id: int, user_id: int):
    return db.query(models.Booking).filter(
        models.Booking.user_id == user_id,
        models.Booking.tenant_id == tenant_id,
        models.Booking.status.in_(['confirmed', 'waitlist'])
    ).all()

def toggle_attendance(db: Session, tenant_id: int, booking_id: int, booked_by_id: Optional[int] = None):
    booking = db.query(models.Booking).filter(
        models.Booking.id == booking_id,
        models.Booking.tenant_id == tenant_id
    ).first()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
        
    booking.attended = not booking.attended
    
    db.commit()
    db.refresh(booking)
    return booking

def bill_booking(db: Session, tenant_id: int, booking_id: int, booked_by_id: Optional[int] = None):
    booking = db.query(models.Booking).filter(
        models.Booking.id == booking_id,
        models.Booking.tenant_id == tenant_id
    ).first()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
        
    appt = booking.appointment
    if not appt or not appt.training_type_id:
        raise HTTPException(status_code=400, detail="Diesem Termin ist keine Leistung zugeordnet.")
        
    user = booking.user
    training_type = appt.training_type
    if not training_type:
         raise HTTPException(status_code=400, detail="Zugeordnete Leistung nicht gefunden.")
         
    price = training_type.default_price
    
    if user.balance < price:
        raise HTTPException(status_code=400, detail=f"Ungenügendes Guthaben ({user.balance}€). Erforderlich: {price}€")
        
    # NEU: Double-Billing Check
    existing_tx = db.query(models.Transaction).filter(
        models.Transaction.user_id == user.id,
        models.Transaction.tenant_id == tenant_id,
        models.Transaction.description == f"Abrechnung: {appt.title}"
    ).first()
    if existing_tx:
        raise HTTPException(status_code=400, detail="Bereits abgerechnet.")

    # Transaktion erstellen
    transaction = models.Transaction(
        tenant_id=tenant_id,
        user_id=user.id,
        type=training_type.name,
        description=f"Abrechnung: {appt.title}",
        amount=-price,
        balance_after=user.balance - price,
        booked_by_id=booked_by_id or user.id # Fallback auf User selbst, falls System-Aktion ohne ID
    )
    user.balance -= price
    
    db.add(transaction)
    
    # WICHTIG: Prüfen ob Auto-Progress aktiv ist bevor Achievement erstellt wird
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    config = tenant.config or {}
    
    if config.get('auto_progress_enabled'):
        existing_achievement = db.query(models.Achievement).filter(
            models.Achievement.user_id == user.id,
            models.Achievement.training_type_id == training_type.id,
            models.Achievement.date_achieved == appt.start_time
        ).first()
        
        if not existing_achievement:
            create_achievement(db, user.id, tenant_id, training_type.id, transaction_id=transaction.id, date_achieved=appt.start_time)
            db.flush() 
    
    db.commit()
    db.refresh(user)
    return booking

def bill_all_participants(db: Session, tenant_id: int, appointment_id: int, booked_by_id: Optional[int] = None):
    bookings = db.query(models.Booking).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.status == 'confirmed',
        models.Booking.attended == True,
        models.Booking.tenant_id == tenant_id
    ).all()
    
    results = []
    for booking in bookings:
        try:
            bill_booking(db, tenant_id, booking.id, booked_by_id=booked_by_id)
            results.append({"booking_id": booking.id, "status": "success"})
        except HTTPException as e:
            results.append({"booking_id": booking.id, "status": "error", "detail": e.detail})
            
    return results

def grant_progress_booking(db: Session, tenant_id: int, booking_id: int):
    booking = db.query(models.Booking).filter(
        models.Booking.id == booking_id,
        models.Booking.tenant_id == tenant_id
    ).first()
    if not booking: raise HTTPException(404, "Booking not found")
    
    appt = booking.appointment
    if not appt or not appt.training_type_id:
        raise HTTPException(400, "Diesem Termin ist keine Leistung zugeordnet.")
    
    # Check if exists
    existing = db.query(models.Achievement).filter(
        models.Achievement.user_id == booking.user_id,
        models.Achievement.training_type_id == appt.training_type_id,
        models.Achievement.date_achieved == appt.start_time
    ).first()
    
    if not existing:
        create_achievement(db, booking.user_id, tenant_id, appt.training_type_id, date_achieved=appt.start_time)
        db.commit()
    return booking

def grant_all_progress(db: Session, tenant_id: int, appointment_id: int):
    bookings = db.query(models.Booking).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.status == 'confirmed',
        models.Booking.attended == True,
        models.Booking.tenant_id == tenant_id
    ).all()
    
    results = []
    for booking in bookings:
        try:
            grant_progress_booking(db, tenant_id, booking.id)
            results.append({"booking_id": booking.id, "status": "success"})
        except HTTPException as e:
            results.append({"booking_id": booking.id, "status": "error", "detail": e.detail})
    return results

# --- CHECK AND SEND REMINDERS ---

def check_and_send_reminders(db: Session):
    """
    Sucht nach anstehenden Terminen und sendet Erinnerungen basierend auf den User-Einstellungen.
    """
    now = datetime.now(timezone.utc)
    
    # Performance: Wir laden nur Buchungen der nächsten 24h
    upcoming_limit = now + timedelta(hours=24)
    
    # Lade alle bestätigten Buchungen für zukünftige Termine
    bookings = db.query(models.Booking).join(models.Appointment).join(models.User).filter(
        models.Booking.status == 'confirmed',
        models.Appointment.start_time > now,
        models.Appointment.start_time <= upcoming_limit
    ).all()
    
    sent_count = 0
    
    for booking in bookings:
        user = booking.user
        appt = booking.appointment
        
        # User-spezifischer Offset (default 60 Min)
        offset = user.reminder_offset_minutes if user.reminder_offset_minutes is not None else 60
        
        # Wann soll die Erinnerung gesendet werden?
        reminder_time = appt.start_time - timedelta(minutes=offset)
        
        # Toleranzfenster für den Cronjob (z.B. +/- 5 Min)
        window_start = reminder_time - timedelta(minutes=5)
        window_end = reminder_time + timedelta(minutes=5)
        
        if window_start <= now <= window_end:
            notify_user(
                db=db,
                user_id=user.id,
                type="reminder",
                title="Erinnerung: Termin beginnt bald",
                message=f"Dein Termin '{appt.title}' beginnt in ca. {offset} Minuten.",
                url="/bookings",
                details={
                    "Kurs": appt.title,
                    "Beginn": format_datetime_de(appt.start_time),
                    "Ort": appt.location or "-"
                }
            )
            sent_count += 1
            
    return sent_count

# --- NEWSLETTER LOGIC ---

def add_newsletter_subscriber(db: Session, email: str, source: str):
    subscriber = db.query(models.NewsletterSubscriber).filter(models.NewsletterSubscriber.email == email).first()
    
    if subscriber:
        # Falls User existiert aber abgemeldet war -> Reaktivieren
        if not subscriber.is_subscribed:
            subscriber.is_subscribed = True
            subscriber.unsubscribed_at = None
            subscriber.source = source # Quelle aktualisieren
            db.commit()
            db.refresh(subscriber)
        return subscriber
    else:
        # Neuen Subscriber anlegen
        new_subscriber = models.NewsletterSubscriber(
            email=email,
            source=source,
            is_subscribed=True
        )
        db.add(new_subscriber)
        db.commit()
        db.refresh(new_subscriber)
        return new_subscriber

def unsubscribe_newsletter(db: Session, email: str):
    subscriber = db.query(models.NewsletterSubscriber).filter(models.NewsletterSubscriber.email == email).first()
    if subscriber:
        subscriber.is_subscribed = False
        subscriber.unsubscribed_at = func.now()
        db.commit()
        db.refresh(subscriber)
        return True

# --- NEWS ---

# app/crud.py

def create_news_post(db: Session, post: schemas.NewsPostCreate, author_id: int, tenant_id: int):
    # Fetch target objects
    target_levels = []
    if post.target_level_ids:
        target_levels = db.query(models.Level).filter(
            models.Level.id.in_(post.target_level_ids),
            models.Level.tenant_id == tenant_id
        ).all()
        
    target_appointments = []
    if post.target_appointment_ids:
        target_appointments = db.query(models.Appointment).filter(
            models.Appointment.id.in_(post.target_appointment_ids),
            models.Appointment.tenant_id == tenant_id
        ).all()

    db_post = models.NewsPost(
        tenant_id=tenant_id,
        created_by_id=author_id,
        title=post.title,
        content=post.content,
        image_url=post.image_url,
        target_levels=target_levels,
        target_appointments=target_appointments
    )
    db.add(db_post)
    db.commit()
    db.refresh(db_post)

    # --- NEU: Broadcast-Logik ---
    from .notification_service import notify_user
    
    recipient_ids = set()
    has_specific_targets = False
    
    # 1. Zielgruppe: Level
    if db_post.target_levels:
        has_specific_targets = True
        level_ids = [l.id for l in db_post.target_levels]
        users_in_levels = db.query(models.User.id).filter(
            models.User.tenant_id == tenant_id,
            models.User.is_active == True,
            models.User.current_level_id.in_(level_ids)
        ).all()
        for u in users_in_levels:
            recipient_ids.add(u.id)

    # 2. Zielgruppe: Termin-Teilnehmer (nur bestätigte)
    if db_post.target_appointments:
        has_specific_targets = True
        appt_ids = [a.id for a in db_post.target_appointments]
        users_in_appts = db.query(models.Booking.user_id).filter(
            models.Booking.tenant_id == tenant_id,
            models.Booking.status == 'confirmed',
            models.Booking.appointment_id.in_(appt_ids)
        ).all()
        for u in users_in_appts:
            recipient_ids.add(u.user_id)

    # 3. Fallback: Keine Zielgruppe = Alle Kunden
    if not has_specific_targets:
        all_customers = db.query(models.User.id).filter(
            models.User.tenant_id == tenant_id,
            models.User.is_active == True,
            models.User.role.in_(['kunde', 'customer'])
        ).all()
        for u in all_customers:
            recipient_ids.add(u.id)

    # 4. Senden (Loop durch alle Empfänger)
    print(f"DEBUG: Sende News an {len(recipient_ids)} Empfänger.")
    
    for uid in recipient_ids:
        # Autor überspringen (optional)
        if uid == author_id: 
            continue
            
        notify_user(
            db=db,
            user_id=uid,
            type="news",
            title=f"Neuigkeit: {db_post.title}",
            message=f"{db_post.content[:100]}..." if len(db_post.content) > 100 else db_post.content,
            url="/news",
            details={
                "Titel": db_post.title
            }
        )

    return db_post

def get_news_posts(db: Session, tenant_id: int, current_user: models.User, skip: int = 0, limit: int = 50):
    query = db.query(models.NewsPost).options(
        joinedload(models.NewsPost.author),
        joinedload(models.NewsPost.target_levels),
        joinedload(models.NewsPost.target_appointments)
    ).filter(
        models.NewsPost.tenant_id == tenant_id
    )

    # Filter for customers
    if current_user.role in ['kunde', 'customer']:
        # Post is visible if:
        # 1. No target levels AND no target trainings (Target: All)
        # OR 2. Current user's level is in target_levels
        # OR 3. User has a confirmed booking for an appointment in target_appointments
        
        user_bookings = db.query(models.Booking.appointment_id).filter(
            models.Booking.user_id == current_user.id,
            models.Booking.tenant_id == tenant_id,
            models.Booking.status == "confirmed"
        ).all()
        user_appointment_ids = [b[0] for b in user_bookings]

        # In SQL this is a bit more complex with many-to-many. 
        # Easier with a subquery or OR conditions if we can represent them cleanly.
        # Alternatively, we fetch and filter in Python, but that's not ideal for pagination.
        
        # Let's try to do it in SQL.
        # Condition 1: No targets
        cond_no_targets = and_(
            ~models.NewsPost.target_levels.any(),
            ~models.NewsPost.target_appointments.any()
        )
        
        # Condition 2: Matching level
        cond_matching_level = False
        if current_user.current_level_id:
            cond_matching_level = models.NewsPost.target_levels.any(models.Level.id == current_user.current_level_id)
            
        # Condition 3: Matching appointment
        cond_matching_appointment = False
        if user_appointment_ids:
            cond_matching_appointment = models.NewsPost.target_appointments.any(models.Appointment.id.in_(user_appointment_ids))
            
        filters = [cond_no_targets]
        if cond_matching_level is not False: filters.append(cond_matching_level)
        if cond_matching_appointment is not False: filters.append(cond_matching_appointment)
        
        query = query.filter(or_(*filters))

    posts = query.order_by(models.NewsPost.created_at.desc()).offset(skip).limit(limit).all()
    
    # Map target IDs back to schema
    for post in posts:
        post.target_level_ids = [l.id for l in post.target_levels]
        post.target_appointment_ids = [a.id for a in post.target_appointments]
        
    return posts

def update_news_post(db: Session, post_id: int, tenant_id: int, update: schemas.NewsPostUpdate):
    db_post = db.query(models.NewsPost).filter(
        models.NewsPost.id == post_id,
        models.NewsPost.tenant_id == tenant_id
    ).first()
    
    if not db_post:
        return None

    update_data = update.model_dump(exclude_unset=True)
    
    if "target_level_ids" in update_data:
        level_ids = update_data.pop("target_level_ids")
        levels = db.query(models.Level).filter(
            models.Level.id.in_(level_ids),
            models.Level.tenant_id == tenant_id
        ).all()
        db_post.target_levels = levels

    if "target_appointment_ids" in update_data:
        appt_ids = update_data.pop("target_appointment_ids")
        appts = db.query(models.Appointment).filter(
            models.Appointment.id.in_(appt_ids),
            models.Appointment.tenant_id == tenant_id
        ).all()
        db_post.target_appointments = appts

    for key, value in update_data.items():
        setattr(db_post, key, value)

    db.add(db_post)
    db.commit()
    db.refresh(db_post)
    
    # Map target IDs back to schema
    db_post.target_level_ids = [l.id for l in db_post.target_levels]
    db_post.target_appointment_ids = [a.id for a in db_post.target_appointments]
    
    return db_post

def delete_news_post(db: Session, post_id: int, tenant_id: int):
    db_post = db.query(models.NewsPost).filter(
        models.NewsPost.id == post_id,
        models.NewsPost.tenant_id == tenant_id
    ).first()
    
    if not db_post:
        return False

    db.delete(db_post)
    db.commit()
    return True

# --- CHAT ---

def create_chat_message(db: Session, msg: schemas.ChatMessageCreate, sender_id: int, tenant_id: int):
    # Verify receiver exists and belongs to same tenant
    receiver = get_user(db, msg.receiver_id, tenant_id)
    if not receiver:
        raise HTTPException(404, "Receiver not found")

    new_message = models.ChatMessage(
        tenant_id=tenant_id,
        sender_id=sender_id,
        receiver_id=msg.receiver_id,
        content=msg.content,
        file_url=msg.file_url,
        file_type=msg.file_type,
        file_name=msg.file_name
    )
    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    # Push-Benachrichtigung an den Empfänger senden
    from .notification_service import notify_user
    notify_user(
        db=db,
        user_id=new_message.receiver_id,
        type="chat",
        title=f"Neue Nachricht von {new_message.sender.name}",
        message=new_message.content[:100] if not new_message.file_url else "Datei gesendet",
        url=f"/chat/{new_message.sender_id}"
    )

    return new_message

def get_chat_history(db: Session, tenant_id: int, user1_id: int, user2_id: int, limit: int = 100):
    """
    Holt die Chat-Historie zwischen zwei Nutzern (egal wer Sender/Empfänger ist).
    Sortiert nach Datum aufsteigend (älteste zuerst).
    """
    messages = db.query(models.ChatMessage).filter(
        models.ChatMessage.tenant_id == tenant_id,
        # (Sender = U1 AND Receiver = U2) OR (Sender = U2 AND Receiver = U1)
        and_(
            models.ChatMessage.sender_id.in_([user1_id, user2_id]),
            models.ChatMessage.receiver_id.in_([user1_id, user2_id])
        )
    ).order_by(models.ChatMessage.created_at.asc()).limit(limit).all()
    
    return messages

def get_chat_conversations_for_user(db: Session, user: models.User):
    """
    Ermittelt alle Gesprächspartner für den aktuellen User.
    """
    # 1. Partner IDs ermitteln (User, mit denen ich interagiert habe)
    sent_to = db.query(models.ChatMessage.receiver_id).filter(
        models.ChatMessage.sender_id == user.id
    )
    received_from = db.query(models.ChatMessage.sender_id).filter(
        models.ChatMessage.receiver_id == user.id
    )
    
    partner_ids_query = sent_to.union(received_from)
    
    partners = db.query(models.User).filter(
        models.User.id.in_(partner_ids_query)
    ).all()
    
    results = []
    # Workaround um datetime import fehler zu vermeiden falls nicht vorhanden, 
    # wir nutzen einfach None check beim sortieren.
    from datetime import datetime
    
    for partner in partners:
        # Letzte Nachricht holen
        last_msg = db.query(models.ChatMessage).filter(
            or_(
                and_(models.ChatMessage.sender_id == user.id, models.ChatMessage.receiver_id == partner.id),
                and_(models.ChatMessage.sender_id == partner.id, models.ChatMessage.receiver_id == user.id)
            )
        ).order_by(models.ChatMessage.created_at.desc()).first()
        
        # Ungelesene zählen (nur empfangene)
        unread = db.query(models.ChatMessage).filter(
            models.ChatMessage.sender_id == partner.id,
            models.ChatMessage.receiver_id == user.id,
            models.ChatMessage.is_read == False
        ).count()
        
        results.append({
            "user": partner,
            "last_message": last_msg,
            "unread_count": unread
        })
    
    # Sortieren nach Datum der letzten Nachricht (neueste oben)
    # Verwende eine Fallback-Zeit für Sortierung wenn keine Nachricht da ist (sollte theoretisch nicht passieren wenn partners via messages gefunden wurden)
    results.sort(key=lambda x: x["last_message"].created_at if x["last_message"] else datetime.min, reverse=True)
    
    return results

def mark_messages_as_read(db: Session, tenant_id: int, user_id: int, other_user_id: int):
    """
    Markiert alle Nachrichten VON other_user_id AN user_id als gelesen.
    """
    db.query(models.ChatMessage).filter(
        models.ChatMessage.tenant_id == tenant_id,
        models.ChatMessage.sender_id == other_user_id,
        models.ChatMessage.receiver_id == user_id,
        models.ChatMessage.is_read == False
    ).update({"is_read": True})
    db.commit()


def get_chat_conversations(db: Session, tenant_id: int):
    """
    Für Admins: Gibt eine Liste aller User zurück, mit denen es Nachrichten gibt.
    Inkl. der letzten Nachricht und Ungelesen-Status.
    """
    # 1. Finde alle User-IDs, die Nachrichten gesendet oder empfangen haben (außer Admins/Mitarbeiter untereinander ist weniger relevant, hier Fokus auf Kunden)
    # Wir holen 'Kunden', die entweder gesendet haben oder empfangen haben.
    
    # Subquery für letzte Nachricht pro Konversation wäre komplex.
    # Einfacher: Wir holen alle Kunden des Tenants und schauen, ob Chats existieren.
    # Da das teuer sein kann bei vielen Kunden, machen wir es über die Nachrichten Tabelle.
    
    # Alle UserIDs die involviert sind
    senders = db.query(models.ChatMessage.sender_id).filter(models.ChatMessage.tenant_id == tenant_id).distinct()
    receivers = db.query(models.ChatMessage.receiver_id).filter(models.ChatMessage.tenant_id == tenant_id).distinct()
    
    user_ids = set()
    for s in senders: user_ids.add(s[0])
    for r in receivers: user_ids.add(r[0])
    
    # User Details laden
    users = db.query(models.User).filter(
        models.User.id.in_(user_ids), 
        models.User.role.in_(['kunde', 'customer']) # Nur Kunden anzeigen
    ).all()
    
    conversations = []
    
    for user in users:
        # Letzte Nachricht holen
        last_msg = db.query(models.ChatMessage).filter(
            models.ChatMessage.tenant_id == tenant_id,
            or_(
                models.ChatMessage.sender_id == user.id,
                models.ChatMessage.receiver_id == user.id
            )
        ).order_by(models.ChatMessage.created_at.desc()).first()
        
        # Ungelesene Zählen (Nachrichten VOM Kunden AN Irgendwen (Admins))
        # Da wir im Admin Kontext sind: Nachrichten die der Kunde geschickt hat und die noch nicht gelesen sind.
        unread_count = db.query(models.ChatMessage).filter(
            models.ChatMessage.tenant_id == tenant_id,
            models.ChatMessage.sender_id == user.id,
            models.ChatMessage.is_read == False
        ).count()
        
        conversations.append({
            "user": user,
            "last_message": last_msg,
            "unread_count": unread_count
        })
        
    # Sortieren nach Datum der letzten Nachricht (neueste oben)
    conversations.sort(key=lambda x: x["last_message"].created_at if x["last_message"] else datetime.min, reverse=True)
    
    return conversations

# --- APP STATUS ---

def get_app_status(db: Session, tenant_id: int):
    status = db.query(models.AppStatus).filter(models.AppStatus.tenant_id == tenant_id).first()
    if not status:
        # Initialen Status erstellen wenn nicht vorhanden
        status = models.AppStatus(tenant_id=tenant_id, status="active", message="")
        db.add(status)
        db.commit()
        db.refresh(status)
    return status

def update_app_status(db: Session, tenant_id: int, status_update: schemas.AppStatusUpdate):
    db_status = get_app_status(db, tenant_id)
    db_status.status = status_update.status
    db_status.message = status_update.message
    db_status.updated_at = func.now()
    
    db.add(db_status)
    db.commit()
    db.refresh(db_status)
    
    # --- NEU: Broadcast Benachrichtigung ---
    from .notification_service import notify_user
    
    # Alle aktiven User dieses Tenants holen
    active_users = db.query(models.User.id).filter(
        models.User.tenant_id == tenant_id,
        models.User.is_active == True
    ).all()
    
    for u in active_users:
        notify_user(
            db=db,
            user_id=u.id,
            type="alert",
            title="Status Update",
            message=status_update.message or f"Der Status der App hat sich auf '{status_update.status}' geändert.",
            url="/",
            details={
                "Neuer Status": status_update.status,
                "Nachricht": status_update.message or "-"
            }
        )
        
    return db_status
