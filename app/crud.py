from sqlalchemy.orm import Session
from . import models, schemas, auth
from fastapi import HTTPException
import secrets
from typing import List, Optional

# In backend/app/crud.py (ganz oben)

LEVEL_REQUIREMENTS = {
  # Level 1 (Welpen) hat keine Anforderungen für den Aufstieg.
  2: [{"id": 'group_class', "name": 'Gruppenstunde', "required": 6}, {"id": 'exam', "name": 'Prüfung', "required": 1}],
  3: [{"id": 'group_class', "name": 'Gruppenstunde', "required": 6}, {"id": 'exam', "name": 'Prüfung', "required": 1}],
  4: [{"id": 'social_walk', "name": 'Social Walk', "required": 6}, {"id": 'tavern_training', "name": 'Wirtshaustraining', "required": 2}, {"id": 'exam', "name": 'Prüfung', "required": 1}],
  5: [{"id": 'exam', "name": 'Prüfung', "required": 1}],
}

# In backend/app/crud.py

DOGLICENSE_PREREQS = [
    {"id": 'lecture_bonding', "name": 'Vortrag Bindung & Beziehung', "required": 1},
    {"id": 'lecture_hunting', "name": 'Vortrag Jagdverhalten', "required": 1},
    {"id": 'ws_communication', "name": 'WS Kommunikation & Körpersprache', "required": 1},
    {"id": 'ws_stress', "name": 'WS Stress & Impulskontrolle', "required": 1},
    {"id": 'theory_license', "name": 'Theorieabend Hundeführerschein', "required": 1},
    {"id": 'first_aid', "name": 'Erste-Hilfe-Kurs', "required": 1},
]

# --- USER ---
def get_user(db: Session, user_id: int):
    return db.query(models.User).filter(models.User.id == user_id).first()


def get_user_by_email(db: Session, email: str):
    return db.query(models.User).filter(models.User.email == email).first()


def get_users(db: Session, skip: int = 0, limit: int = 100, portfolio_of_user_id: Optional[int] = None):
    query = db.query(models.User)
    if portfolio_of_user_id:
        # Finde alle User-IDs, mit denen der Mitarbeiter Transaktionen hatte
        customer_ids_with_transactions = db.query(models.Transaction.user_id).filter(models.Transaction.booked_by_id == portfolio_of_user_id).distinct()
        # Filter die User-Liste auf diese IDs
        query = query.filter(models.User.id.in_([c[0] for c in customer_ids_with_transactions]))

    return query.order_by(models.User.name).offset(skip).limit(limit).all()


def search_users(db: Session, search_term: str):
    return db.query(models.User).filter(models.User.name.like(f"%{search_term}%")).all()


def create_user(db: Session, user: schemas.UserCreate):
    # NEU: Wenn kein Passwort übergeben wird, erstelle ein sicheres Zufallspasswort
    if not user.password:
        user.password = secrets.token_urlsafe(16)

    hashed_password = auth.get_password_hash(user.password)
    db_user = models.User(
        email=user.email,
        name=user.name,
        role=user.role,
        is_active=user.is_active,
        balance=user.balance,
        phone=user.phone,
        level_id=user.level_id,
        hashed_password=hashed_password
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    for dog_data in user.dogs:
        db_dog = models.Dog(**dog_data.model_dump(), owner_id=db_user.id)
        db.add(db_dog)
    db.commit()
    db.refresh(db_user)
    return db_user


# In backend/app/crud.py

# In backend/app/crud.py

def are_prerequisites_met_for_exam(db: Session, customer: models.User) -> bool:
    """
    Prüft, ob ein Kunde alle Nicht-Prüfungs-Anforderungen für sein aktuelles Level
    oder für den Hundeführerschein (Level 5) erfüllt hat.
    """
    current_level_id = customer.level_id

    # *** NEU: Sonderlogik für den Hundeführerschein (Level 5) ***
    if current_level_id == 5:
        print("DEBUG: Prüfe Voraussetzungen für Level 5 (Hundeführerschein).")
        prereqs = DOGLICENSE_PREREQS
    else:
        # Bestehende Logik für alle anderen Level
        requirements_for_level = LEVEL_REQUIREMENTS.get(current_level_id, [])
        if not requirements_for_level:
            return True  # Keine Anforderungen, also ist die Prüfung erlaubt.
        prereqs = [req for req in requirements_for_level if req.get("id") != 'exam']

    if not prereqs:
        return True  # Es gibt keine Voraussetzungen außer der Prüfung.

    # Zähle alle bisherigen, unverbrauchten Leistungen des Kunden.
    unconsumed_achievements = db.query(models.Achievement).filter(
        models.Achievement.user_id == customer.id,
        models.Achievement.is_consumed == False
    ).all()

    achievement_counts = {}
    for ach in unconsumed_achievements:
        req_id = ach.requirement_id
        achievement_counts[req_id] = achievement_counts.get(req_id, 0) + 1

    # Prüfe für jede Anforderung, ob die benötigte Anzahl erreicht ist.
    for req in prereqs:
        req_id = req.get("id")
        required_amount = req.get("required")
        if achievement_counts.get(req_id, 0) < required_amount:
            print(
                f"DEBUG: Voraussetzung '{req_id}' nicht erfüllt. Benötigt: {required_amount}, Vorhanden: {achievement_counts.get(req_id, 0)}")
            return False  # Eine Voraussetzung ist nicht erfüllt.

    print("DEBUG: Alle Voraussetzungen für die Prüfung sind erfüllt.")
    return True  # Alle Voraussetzungen sind erfüllt.

def update_user(db: Session, user_id: int, user: schemas.UserUpdate):
    db_user = get_user(db, user_id=user_id)
    if not db_user:
        return None

    # Lade die Update-Daten
    update_data = user.model_dump(exclude_unset=True)

    # Wenn ein neues Passwort mitgesendet wurde, hashe es und entferne es aus den restlichen Daten
    if "password" in update_data and update_data["password"]:
        hashed_password = auth.get_password_hash(update_data.pop("password"))
        db_user.hashed_password = hashed_password

    # Aktualisiere die restlichen Felder
    for key, value in update_data.items():
        setattr(db_user, key, value)

    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def update_user_vip_status(db: Session, user_id: int, is_vip: bool):
    db_user = get_user(db, user_id=user_id)
    if not db_user:
        return None
    db_user.is_vip = is_vip
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def update_user_expert_status(db: Session, user_id: int, is_expert: bool):
    db_user = get_user(db, user_id=user_id)
    if not db_user:
        return None
    db_user.is_expert = is_expert
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

# In backend/app/crud.py
def update_user_status(db: Session, user_id: int, status: schemas.UserStatusUpdate):
    db_user = get_user(db, user_id=user_id)
    if not db_user:
        return None

    update_data = status.model_dump(exclude_unset=True)

    # NEUE REGEL: Wenn ein Status auf True gesetzt wird, wird der andere auf False gesetzt.
    if update_data.get("is_vip") is True:
        db_user.is_expert = False
    elif update_data.get("is_expert") is True:
        db_user.is_vip = False

    # Übernehme die Änderungen aus dem Request
    for key, value in update_data.items():
        setattr(db_user, key, value)

    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def delete_user(db: Session, user_id: int):
    db_user = get_user(db, user_id=user_id)
    if not db_user:
        return None
    db.delete(db_user)
    db.commit()
    return {"ok": True}

# --- TRANSACTION ---
# In backend/app/crud.py

def create_transaction(db: Session, transaction: schemas.TransactionCreate, booked_by: models.User):
    customer = get_user(db, user_id=transaction.user_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    # Bonus Logic
    amount_to_add = transaction.amount
    bonus = 0
    if transaction.type == "Aufladung":  # Bonus nur bei Aufladungen
        if amount_to_add >= 300:
            bonus = 150
        elif amount_to_add >= 150:
            bonus = 30
        elif amount_to_add >= 100:
            bonus = 15
        elif amount_to_add >= 50:
            bonus = 5

    total_change = amount_to_add + bonus

    # Update customer balance
    customer.balance += total_change
    db.add(customer)

    # Transaktion in der DB anlegen
    db_transaction = models.Transaction(
        user_id=customer.id,
        type=transaction.type,
        description=transaction.description,
        amount=total_change,
        balance_after=customer.balance,
        booked_by_id=booked_by.id
    )
    db.add(db_transaction)
    db.flush()  # Wichtig, um eine ID für die Transaktion zu bekommen

    # *** HIER IST DIE NEUE LOGIK FÜR ACHIEVEMENTS ***
    if transaction.requirement_id:
        can_create_achievement = True

        # Sonderprüfung für Prüfungen
        if transaction.requirement_id == 'exam':
            print(f"DEBUG: Prüfungs-Achievement wird geprüft für User {customer.id} in Level {customer.level_id}.")
            if not are_prerequisites_met_for_exam(db, customer):
                can_create_achievement = False
                print(f"DEBUG: Voraussetzungen für Prüfung nicht erfüllt. Achievement wird NICHT erstellt.")

        # Achievement nur erstellen, wenn die Prüfung erlaubt ist ODER es keine Prüfung ist.
        if can_create_achievement:
            print(f"DEBUG: Achievement '{transaction.requirement_id}' wird für User {customer.id} erstellt.")
            create_achievement(
                db,
                user_id=customer.id,
                requirement_id=transaction.requirement_id,
                transaction_id=db_transaction.id
            )

    db.commit()
    db.refresh(db_transaction)
    return db_transaction

def get_transactions(db: Session, skip: int = 0, limit: int = 100):
    """Holt eine Liste von Transaktionen, die neuesten zuerst."""
    return db.query(models.Transaction).order_by(models.Transaction.date.desc()).offset(skip).limit(limit).all()


def get_transactions_for_user(db: Session, user_id: int, for_staff: bool = False):
    """
    Holt Transaktionen.
    - Wenn for_staff=False, holt es alle Transaktionen des Kunden (user_id).
    - Wenn for_staff=True, holt es alle Transaktionen, die vom Mitarbeiter (user_id) gebucht wurden.
    """
    if for_staff:
        # Filter nach der 'booked_by_id' Spalte für Mitarbeiter
        return db.query(models.Transaction).filter(models.Transaction.booked_by_id == user_id).order_by(models.Transaction.date.desc()).all()
    else:
        # Filter nach der 'user_id' Spalte für Kunden
        return db.query(models.Transaction).filter(models.Transaction.user_id == user_id).order_by(models.Transaction.date.desc()).all()
# --- ACHIEVEMENT ---
def create_achievement(db: Session, user_id: int, requirement_id: str, transaction_id: int):
    # Die alte "exists"-Prüfung wurde entfernt.
    # Es wird jetzt immer ein neuer Eintrag erstellt.
    db_achievement = models.Achievement(
        user_id=user_id,
        requirement_id=requirement_id,
        transaction_id=transaction_id
    )
    db.add(db_achievement)
    return db_achievement

# --- USER LEVEL ---
def update_user_level(db: Session, user_id: int, new_level_id: int):
    db_user = get_user(db, user_id=user_id)
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    dog_license_prereq_ids = {req['id'] for req in DOGLICENSE_PREREQS}

    unconsumed_achievements = db.query(models.Achievement).filter_by(
        user_id=user_id, is_consumed=False
    ).all()

    for ach in unconsumed_achievements:
        # Zusatzveranstaltungen werden NICHT verbraucht, alles andere schon.
        if ach.requirement_id not in dog_license_prereq_ids:
            ach.is_consumed = True
            db.add(ach)

    db_user.level_id = new_level_id
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def get_dog(db: Session, dog_id: int):
    return db.query(models.Dog).filter(models.Dog.id == dog_id).first()

def update_dog(db: Session, dog_id: int, dog: schemas.DogBase):
    db_dog = get_dog(db, dog_id=dog_id)
    if not db_dog:
        return None

    update_data = dog.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_dog, key, value)

    db.add(db_dog)
    db.commit()
    db.refresh(db_dog)
    return db_dog

# In backend/app/crud.py

def create_document(db: Session, user_id: int, file_name: str, file_type: str, file_path: str):
    db_doc = models.Document(
        user_id=user_id, file_name=file_name, file_type=file_type, file_path=file_path
    )
    db.add(db_doc)
    db.commit()
    db.refresh(db_doc)
    return db_doc

def get_document(db: Session, document_id: int):
    return db.query(models.Document).filter(models.Document.id == document_id).first()

def delete_document(db: Session, document_id: int):
    db_doc = get_document(db, document_id)
    if db_doc:
        db.delete(db_doc)
        db.commit()
        return True
    return False

def create_dog_for_user(db: Session, dog: schemas.DogCreate, user_id: int):
    db_dog = models.Dog(**dog.model_dump(), owner_id=user_id)
    db.add(db_dog)
    db.commit()
    db.refresh(db_dog)
    return db_dog

def delete_dog(db: Session, dog_id: int):
    db_dog = get_dog(db, dog_id=dog_id)
    if not db_dog:
        return None
    db.delete(db_dog)
    db.commit()
    return {"ok": True}