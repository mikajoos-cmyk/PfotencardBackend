from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import and_, func, or_, case
from . import models, schemas, auth
from fastapi import HTTPException
import secrets
from typing import List, Optional

# --- TENANT & CONFIGURATION ---

def get_tenant_by_subdomain(db: Session, subdomain: str):
    return db.query(models.Tenant).filter(models.Tenant.subdomain == subdomain).first()

def get_app_config(db: Session, tenant_id: int) -> schemas.AppConfig:
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
        
    levels = db.query(models.Level).options(
        joinedload(models.Level.requirements).joinedload(models.LevelRequirement.training_type)
    ).filter(models.Level.tenant_id == tenant_id).order_by(models.Level.rank_order).all()
    
    training_types = db.query(models.TrainingType).filter(
        models.TrainingType.tenant_id == tenant_id
    ).all()
    
    return schemas.AppConfig(
        tenant=tenant,
        levels=levels,
        training_types=training_types
    )

def update_tenant_settings(db: Session, tenant_id: int, settings: schemas.SettingsUpdate):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant: raise HTTPException(404, "Tenant not found")

    # 1. Update Tenant Basic Info & Config
    tenant.name = settings.school_name
    
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
        else:
            new_svc = models.TrainingType(
                tenant_id=tenant_id,
                name=s_data.name,
                category=s_data.category,
                default_price=s_data.price
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
                current_level.has_additional_requirements = l_data.has_additional_requirements
        else:
            current_level = models.Level(
                tenant_id=tenant_id,
                name=l_data.name,
                rank_order=l_data.rank_order,
                icon_url=l_data.badge_image,
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
    if not db_dog: return None
    db.delete(db_dog)
    db.commit()
    return {"ok": True}

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

def create_transaction(db: Session, transaction: schemas.TransactionCreate, booked_by_id: int, tenant_id: int):
    user = get_user(db, transaction.user_id, tenant_id)
    if not user: raise HTTPException(404, "User not found")

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


def create_achievement(db: Session, user_id: int, tenant_id: int, training_type_id: int, transaction_id: Optional[int] = None):
    ach = models.Achievement(
        tenant_id=tenant_id,
        user_id=user_id,
        training_type_id=training_type_id,
        transaction_id=transaction_id
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
    return doc

def get_document(db: Session, document_id: int, tenant_id: int):
    return db.query(models.Document).filter(
        models.Document.id == document_id,
        models.Document.tenant_id == tenant_id
    ).first()

def delete_document(db: Session, document_id: int, tenant_id: int):
    doc = get_document(db, document_id, tenant_id)
    if doc:
        db.delete(doc)
        db.commit()
        return True
    return False


# --- APPOINTMENTS & BOOKINGS ---

def create_appointment(db: Session, appointment: schemas.AppointmentCreate, tenant_id: int):
    db_appt = models.Appointment(
        tenant_id=tenant_id,
        title=appointment.title,
        description=appointment.description,
        start_time=appointment.start_time,
        end_time=appointment.end_time,
        location=appointment.location,
        max_participants=appointment.max_participants
    )
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
    return db.query(models.Appointment).filter(
        models.Appointment.id == appointment_id,
        models.Appointment.tenant_id == tenant_id
    ).first()

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
            existing.status = 'confirmed'
            db.commit()
            return existing
        else:
            raise HTTPException(400, "Already booked or on waitlist")

    current_count = db.query(models.Booking).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.status == 'confirmed'
    ).count()
    
    if current_count >= appt.max_participants:
        raise HTTPException(400, "Appointment is full")

    booking = models.Booking(
        tenant_id=tenant_id,
        appointment_id=appointment_id,
        user_id=user_id,
        status="confirmed"
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return booking

def cancel_booking(db: Session, tenant_id: int, appointment_id: int, user_id: int):
    booking = db.query(models.Booking).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.user_id == user_id,
        models.Booking.tenant_id == tenant_id
    ).first()
    
    if not booking:
        raise HTTPException(404, "Booking not found")
        
    booking.status = 'cancelled'
    db.commit()
    return {"ok": True}

def get_participants(db: Session, tenant_id: int, appointment_id: int):
    return db.query(models.Booking).options(joinedload(models.Booking.user)).filter(
        models.Booking.appointment_id == appointment_id,
        models.Booking.tenant_id == tenant_id
    ).all()

def get_user_bookings(db: Session, tenant_id: int, user_id: int):
    return db.query(models.Booking).filter(
        models.Booking.user_id == user_id,
        models.Booking.tenant_id == tenant_id,
        models.Booking.status == 'confirmed'
    ).all()

def toggle_attendance(db: Session, tenant_id: int, booking_id: int):
    booking = db.query(models.Booking).filter(
        models.Booking.id == booking_id,
        models.Booking.tenant_id == tenant_id
    ).first()
    
    if not booking:
        raise HTTPException(404, "Booking not found")
        
    booking.attended = not booking.attended
    db.commit()
    db.refresh(booking)
    return booking

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

def create_news_post(db: Session, post: schemas.NewsPostCreate, author_id: int, tenant_id: int):
    db_post = models.NewsPost(
        tenant_id=tenant_id,
        created_by_id=author_id,
        title=post.title,
        content=post.content,
        image_url=post.image_url
    )
    db.add(db_post)
    db.commit()
    db.refresh(db_post)
    return db_post

def get_news_posts(db: Session, tenant_id: int, skip: int = 0, limit: int = 50):
    posts = db.query(models.NewsPost).options(
        joinedload(models.NewsPost.author)
    ).filter(
        models.NewsPost.tenant_id == tenant_id
    ).order_by(models.NewsPost.created_at.desc()).offset(skip).limit(limit).all()
    
    # Optional: Map author name if needed manually, or let schemas handle it via ORM
    return posts

# --- CHAT ---

def create_chat_message(db: Session, msg: schemas.ChatMessageCreate, sender_id: int, tenant_id: int):
    # Verify receiver exists and belongs to same tenant
    receiver = get_user(db, msg.receiver_id, tenant_id)
    if not receiver:
        raise HTTPException(404, "Receiver not found")

    db_msg = models.ChatMessage(
        tenant_id=tenant_id,
        sender_id=sender_id,
        receiver_id=msg.receiver_id,
        content=msg.content
    )
    db.add(db_msg)
    db.commit()
    db.refresh(db_msg)
    return db_msg

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
