# app/models.py
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Date, Boolean, UniqueConstraint, Table
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import JSONB, UUID

Base = declarative_base()

# --- 1. DER MANDANT (TENANT) ---
class Tenant(Base):
    __tablename__ = 'tenants'
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    subdomain = Column(String(255), unique=True, index=True, nullable=False)
    config = Column(JSONB, default={})  # Speichert Branding, Texte, Features
    plan = Column(String(50), default="starter")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Abo-Laufzeit
    subscription_ends_at = Column(DateTime(timezone=True), nullable=True)
    
    support_email = Column(String(255), nullable=True) # E-Mail für die Endkunden der Schule
    
    # Stripe Integration
    stripe_customer_id = Column(String(255), nullable=True, index=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    
    # NEU: Status direkt in DB speichern für einfachere Abfragen
    stripe_subscription_status = Column(String(50), nullable=True) # active, trialing, past_due, canceled, etc.
    cancel_at_period_end = Column(Boolean, default=False) # True wenn gekündigt zum Laufzeitende
    
    # --- NEU: Datenschutz / AVV Audit Log ---
    avv_accepted_at = Column(DateTime(timezone=True), nullable=True)
    avv_accepted_version = Column(String(50), nullable=True) # z.B. "1.0"
    avv_accepted_by_user_id = Column(Integer, nullable=True) # ID des Admins, der geklickt hat
    # ----------------------------------------
    
    # --- NEU: Vorschau auf nächste Zahlung & Wechsel ---
    next_payment_amount = Column(Float, default=0.0)
    next_payment_date = Column(DateTime(timezone=True), nullable=True)
    upcoming_plan = Column(String(50), nullable=True) # z.B. 'pro' wenn ein Wechsel ansteht
    # ---------------------------------------------------

    # Beziehungen (Ein Tenant hat viele...)
    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    dogs = relationship("Dog", back_populates="tenant", cascade="all, delete-orphan")
    training_types = relationship("TrainingType", back_populates="tenant", cascade="all, delete-orphan")
    levels = relationship("Level", back_populates="tenant", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="tenant", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="tenant", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="tenant", cascade="all, delete-orphan")
    news_posts = relationship("NewsPost", back_populates="tenant", cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="tenant", cascade="all, delete-orphan")


# --- 2. KONFIGURATION (Leistungen & Level) ---
class TrainingType(Base):
    __tablename__ = 'training_types'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    
    name = Column(String(255), nullable=False)  # z.B. "Gruppenstunde"
    category = Column(String(50), nullable=False) # 'training', 'workshop', etc.
    default_price = Column(Float, default=0.0)
    rank_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant", back_populates="training_types")
    requirements = relationship("LevelRequirement", back_populates="training_type")
    achievements = relationship("Achievement", back_populates="training_type")


class Level(Base):
    __tablename__ = 'levels'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    
    name = Column(String(255), nullable=False)  # z.B. "Welpe"
    rank_order = Column(Integer, nullable=False) # 1, 2, 3...
    icon_url = Column(String(512))
    has_additional_requirements = Column(Boolean, default=False) # NEU: Fragt Zusatzleistungen ab
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant", back_populates="levels")
    requirements = relationship("LevelRequirement", back_populates="level", cascade="all, delete-orphan")
    users = relationship("User", back_populates="current_level")


class LevelRequirement(Base):
    __tablename__ = 'level_requirements'
    
    id = Column(Integer, primary_key=True, index=True)
    level_id = Column(Integer, ForeignKey('levels.id', ondelete="CASCADE"), nullable=False)
    training_type_id = Column(Integer, ForeignKey('training_types.id', ondelete="CASCADE"), nullable=False)
    required_count = Column(Integer, default=1)
    is_additional = Column(Boolean, default=False) # NEU: Markiert als Zusatzleistung
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    level = relationship("Level", back_populates="requirements")
    training_type = relationship("TrainingType", back_populates="requirements")


# --- 3. DIE DATEN (Users, Dogs, Transactions) ---
class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    auth_id = Column(UUID, nullable=True) # Verknüpfung zu Supabase Auth
    
    name = Column(String(255), index=True, nullable=False)
    email = Column(String(255), index=True, nullable=False)
    hashed_password = Column(String(255), nullable=True)
    
    role = Column(String(50), nullable=False)
    is_active = Column(Boolean, default=True)
    balance = Column(Float, default=0.0)
    customer_since = Column(DateTime(timezone=True), server_default=func.now())
    phone = Column(String(50), nullable=True)
    
    # Status & Level
    is_vip = Column(Boolean, default=False, nullable=False)
    is_expert = Column(Boolean, default=False, nullable=False)
    current_level_id = Column(Integer, ForeignKey('levels.id', ondelete="SET NULL"), nullable=True)

    @property
    def level_id(self):
        return self.current_level_id

    # WICHTIG: E-Mail muss pro Tenant einzigartig sein, nicht global!
    __table_args__ = (UniqueConstraint('email', 'tenant_id', name='uix_email_tenant'),)

    # Beziehungen
    tenant = relationship("Tenant", back_populates="users")
    current_level = relationship("Level", back_populates="users")
    
    dogs = relationship("Dog", back_populates="owner", cascade="all, delete-orphan")
    
    transactions = relationship("Transaction", 
                              foreign_keys='[Transaction.user_id]', 
                              back_populates="user", 
                              cascade="all, delete-orphan")
    
    booked_transactions = relationship("Transaction", 
                                     foreign_keys='[Transaction.booked_by_id]', 
                                     back_populates="booked_by")
                                     
    achievements = relationship("Achievement", back_populates="user", cascade="all, delete-orphan")
    achievements = relationship("Achievement", back_populates="user", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="user", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="user", cascade="all, delete-orphan")
    push_subscriptions = relationship("PushSubscription", back_populates="user", cascade="all, delete-orphan")


class Dog(Base):
    __tablename__ = 'dogs'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    
    name = Column(String(255), index=True, nullable=False)
    breed = Column(String(255))
    birth_date = Column(Date)
    chip = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant", back_populates="dogs")
    owner = relationship("User", back_populates="dogs")


class Transaction(Base):
    __tablename__ = 'transactions'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    booked_by_id = Column(Integer, ForeignKey('users.id', ondelete="SET NULL"), nullable=True)
    
    date = Column(DateTime(timezone=True), server_default=func.now())
    type = Column(String(255), nullable=False) # "Aufladung" oder Leistungstyp
    description = Column(String(255))
    amount = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    
    # NEU: Speichert den Bonus explizit ab
    bonus = Column(Float, default=0.0)

    tenant = relationship("Tenant", back_populates="transactions")
    user = relationship("User", foreign_keys=[user_id], back_populates="transactions")
    booked_by = relationship("User", foreign_keys=[booked_by_id], back_populates="booked_transactions")


class Achievement(Base):
    __tablename__ = 'achievements'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    
    training_type_id = Column(Integer, ForeignKey('training_types.id', ondelete="SET NULL"), nullable=True)
    
    transaction_id = Column(Integer, ForeignKey('transactions.id', ondelete="CASCADE"), nullable=True)
    date_achieved = Column(DateTime(timezone=True), server_default=func.now())
    is_consumed = Column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="achievements")
    training_type = relationship("TrainingType", back_populates="achievements")


class Document(Base):
    __tablename__ = 'documents'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(100), nullable=False)
    upload_date = Column(DateTime(timezone=True), server_default=func.now())
    file_path = Column(String(512), nullable=False)

    user = relationship("User", back_populates="documents")


# Zuordnungstabellen für Appointment-Zielgruppen
appointment_target_levels = Table(
    'appointment_target_levels',
    Base.metadata,
    Column('appointment_id', Integer, ForeignKey('appointments.id', ondelete="CASCADE"), primary_key=True),
    Column('level_id', Integer, ForeignKey('levels.id', ondelete="CASCADE"), primary_key=True)
)

# --- 4. TERMINVEREINBARUNG (APPOINTMENTS) ---

class Appointment(Base):
    __tablename__ = 'appointments'

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    
    title = Column(String(255), nullable=False)
    description = Column(String(1024), nullable=True)
    
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    location = Column(String(255), nullable=True)
    
    max_participants = Column(Integer, default=10)
    
    trainer_id = Column(Integer, ForeignKey('users.id', ondelete="SET NULL"), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant", back_populates="appointments")
    bookings = relationship("Booking", back_populates="appointment", cascade="all, delete-orphan")
    trainer = relationship("User", foreign_keys=[trainer_id])
    target_levels = relationship("Level", secondary=appointment_target_levels)


class Booking(Base):
    __tablename__ = 'bookings'

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    appointment_id = Column(Integer, ForeignKey('appointments.id', ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    
    status = Column(String(50), default="confirmed") # confirmed, cancelled, waitlist
    attended = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Ein User kann pro Termin nur einmal buchen
    __table_args__ = (UniqueConstraint('appointment_id', 'user_id', name='uix_appointment_user'),)

    tenant = relationship("Tenant", back_populates="bookings")
    appointment = relationship("Appointment", back_populates="bookings")
    user = relationship("User", back_populates="bookings")


# --- 5. MARKETING & NEWSLETTER ---

class NewsletterSubscriber(Base):
    __tablename__ = 'newsletter_subscribers'
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    # Boolean Spalte für Abmeldung (True = Aktiv, False = Abgemeldet)
    is_subscribed = Column(Boolean, default=True, nullable=False)
    # Quelle der Anmeldung (z.B. 'marketing_site', 'school_registration')
    source = Column(String(50), nullable=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# Zuordnungstabellen für News-Zielgruppen
news_target_levels = Table(
    'news_target_levels',
    Base.metadata,
    Column('news_post_id', Integer, ForeignKey('news_posts.id', ondelete="CASCADE"), primary_key=True),
    Column('level_id', Integer, ForeignKey('levels.id', ondelete="CASCADE"), primary_key=True)
)

news_target_appointments = Table(
    'news_target_appointments',
    Base.metadata,
    Column('news_post_id', Integer, ForeignKey('news_posts.id', ondelete="CASCADE"), primary_key=True),
    Column('appointment_id', Integer, ForeignKey('appointments.id', ondelete="CASCADE"), primary_key=True)
)

# --- NEWS & CHAT ENTWICKLUNG ---

class NewsPost(Base):
    __tablename__ = 'news_posts'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    created_by_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    
    title = Column(String(255), nullable=False)
    content = Column(String, nullable=False) # Text Content
    image_url = Column(String(512), nullable=True) # Optionales Bild
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant", back_populates="news_posts")
    author = relationship("User", foreign_keys=[created_by_id])
    
    @property
    def author_name(self):
        return self.author.name if self.author else "Unbekannt"

    # NEU: Zielgruppen
    target_levels = relationship("Level", secondary=news_target_levels)
    target_appointments = relationship("Appointment", secondary=news_target_appointments)


class ChatMessage(Base):
    __tablename__ = 'chat_messages'

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    
    sender_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    receiver_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    
    content = Column(String, nullable=False)
    is_read = Column(Boolean, default=False)
    
    # --- NEU: Datei-Anhang ---
    file_url = Column(String(512), nullable=True)
    file_type = Column(String(50), nullable=True) # z.B. 'image', 'document'
    file_name = Column(String(255), nullable=True)
    # -------------------------
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant", back_populates="chat_messages")
    sender = relationship("User", foreign_keys=[sender_id])
    receiver = relationship("User", foreign_keys=[receiver_id])

class AppStatus(Base):
    __tablename__ = 'app_status'
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False, unique=True)
    
    status = Column(String(50), default="active", nullable=False) # active, cancelled, partial
    message = Column(String(512), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    tenant = relationship("Tenant")

class PushSubscription(Base):
    __tablename__ = 'push_subscriptions'
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), nullable=False)
    # WICHTIG: Subscriptions gehören zum User, nicht zwingend zum Tenant, 
    # aber da deine User tenant-scoped sind, passt das so.
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete="CASCADE"), nullable=False)
    
    # VAPID Daten vom Browser
    endpoint = Column(String, nullable=False)
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="push_subscriptions")