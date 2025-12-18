from sqlalchemy.orm import Session, joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import and_, func
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
    
    # LOGO SPEICHERN: Nur wenn eine URL gesendet wurde
    if settings.logo_url:
        current_config["branding"]["logo_url"] = settings.logo_url
    
    # Wording Updates
    current_config["wording"] = current_config.get("wording", {})
    current_config["wording"]["level"] = settings.level_term
    current_config["wording"]["vip"] = settings.vip_term

    # Balance Updates
    current_config["balance"] = current_config.get("balance", {})
    current_config["balance"]["allow_custom_top_up"] = settings.allow_custom_top_up
    current_config["balance"]["top_up_options"] = [opt.dict() for opt in settings.top_up_options]
    
    # Zuweisen und als geändert markieren (WICHTIG!)
    tenant.config = current_config
    flag_modified(tenant, "config")
    
    # 2. Sync Services (TrainingTypes)
    existing_services = db.query(models.TrainingType).filter(models.TrainingType.tenant_id == tenant_id).all()
    existing_service_ids = {s.id for s in existing_services}
    # FIX: Nur positive IDs gelten als existierend. Negative IDs sind neu.
    payload_service_ids = {s.id for s in settings.services if s.id is not None and s.id > 0}
    
    to_delete_ids = existing_service_ids - payload_service_ids
    if to_delete_ids:
        db.query(models.TrainingType).filter(models.TrainingType.id.in_(to_delete_ids)).delete(synchronize_session=False)
    
    # Mapping speichern: Frontend-ID (negativ) -> Neue DB-ID (positiv)
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
            db.flush() # ID generieren
            
            # Falls eine negative ID übergeben wurde, mapping speichern
            if s_data.id and s_data.id < 0:
                temp_id_mapping[s_data.id] = new_svc.id
    
    db.flush()

    # 3. Sync Levels
    existing_levels = db.query(models.Level).filter(models.Level.tenant_id == tenant_id).all()
    existing_level_ids = {l.id for l in existing_levels}
    # Auch hier: Nur positive IDs gelten als existierend
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
                
                # WICHTIG: Hier prüfen wir, ob die ID gemappt werden muss
                training_id = req_data.training_type_id
                if training_id in temp_id_mapping:
                    training_id = temp_id_mapping[training_id]

                new_req = models.LevelRequirement(
                    level_id=current_level.id,
                    training_type_id=training_id, # Verwende die korrekte, positive ID
                    required_count=req_data.required_count,
                    is_additional=req_data.is_additional
                )
                db.add(new_req)

    db.commit()
    db.refresh(tenant)
    return tenant

# --- USER ---

def get_user(db: Session, user_id: int, tenant_id: int):
    return db.query(models.User).filter(
        models.User.id == user_id, 
        models.User.tenant_id == tenant_id
    ).first()

def get_user_by_email(db: Session, email: str, tenant_id: int):
    return db.query(models.User).filter(
        models.User.email == email, 
        models.User.tenant_id == tenant_id
    ).first()

def get_users(db: Session, tenant_id: int, skip: int = 0, limit: int = 100, portfolio_of_user_id: Optional[int] = None):
    query = db.query(models.User).filter(models.User.tenant_id == tenant_id)
    
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
    # Dies ist eine Hilfsfunktion für manuelles Level-Setzen
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
    # Finde das nächste Level basierend auf rank_order
    next_level = db.query(models.Level).filter(
        models.Level.tenant_id == user.tenant_id,
        models.Level.rank_order > current_level.rank_order
    ).order_by(models.Level.rank_order.asc()).first()

    if not next_level:
        return False

    requirements = db.query(models.LevelRequirement).filter(
        models.LevelRequirement.level_id == current_level.id,
        models.LevelRequirement.is_additional == False # Nur Pflichtanforderungen prüfen
    ).all()

    if not requirements:
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

    for req in requirements:
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
        achievements_to_consume = db.query(models.Achievement).filter(
            models.Achievement.user_id == user.id,
            models.Achievement.tenant_id == tenant_id,
            models.Achievement.training_type_id == req.training_type_id,
            models.Achievement.is_consumed == False
        ).order_by(models.Achievement.date_achieved.asc()).limit(req.required_count).all()
        
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
    # Optional: Bonus Logik hier auch dynamisch machen oder aus Tenant Config laden
    bonus = 0
    # Beispielhafte harte Logik, sollte idealerweise auch aus tenant.config kommen
    if transaction.type == "Aufladung":
        if amount_to_add >= 300: bonus = 150
        elif amount_to_add >= 150: bonus = 30
        elif amount_to_add >= 100: bonus = 15
        elif amount_to_add >= 50: bonus = 5

    total_change = amount_to_add + bonus
    user.balance += total_change
    db.add(user)

    db_tx = models.Transaction(
        tenant_id=tenant_id,
        user_id=user.id,
        booked_by_id=booked_by_id,
        type=transaction.type,
        description=transaction.description,
        amount=total_change,
        balance_after=user.balance
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
    db.add(ach)
    return ach

def get_transactions_for_user(db: Session, user_id: int, tenant_id: int, for_staff: bool = False):
    query = db.query(models.Transaction).filter(models.Transaction.tenant_id == tenant_id)
    
    if for_staff:
        query = query.filter(models.Transaction.booked_by_id == user_id)
    else:
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