# app/schemas.py
from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional, Dict, Any
from datetime import datetime, date
from uuid import UUID

class TenantConfig(BaseModel):
    branding: Dict[str, Any] = {} 
    wording: Dict[str, str] = {} 
    balance: Dict[str, Any] = {} 
    features: Dict[str, bool] = {} 
    active_modules: List[str] = ["news", "documents"]

class TenantBase(BaseModel):
    name: str
    subdomain: str
    support_email: Optional[str] = None
    plan: str = "starter"
    is_active: bool = True
    config: TenantConfig = TenantConfig()

class TenantCreate(TenantBase):
    pass

class Tenant(TenantBase):
    id: int
    created_at: datetime
    subscription_ends_at: Optional[datetime] = None
    
    # NEU: Felder auch im Tenant Schema
    stripe_subscription_status: Optional[str] = None
    cancel_at_period_end: bool = False
    # NEU:
    avv_accepted_at: Optional[datetime] = None
    avv_accepted_version: Optional[str] = None

    class Config:
        from_attributes = True

class TenantStatus(BaseModel):
    exists: bool
    name: Optional[str] = None
    subscription_valid: bool = False
    subscription_ends_at: Optional[datetime] = None
    plan: Optional[str] = None
    has_payment_method: bool = False 
    in_trial: bool = False
    
    # NEU: WICHTIG - Diese Felder müssen hier rein!
    stripe_subscription_status: Optional[str] = None
    cancel_at_period_end: bool = False
    
    # NEU: Vorschau-Daten
    next_payment_amount: Optional[float] = None
    next_payment_date: Optional[datetime] = None
    upcoming_plan: Optional[str] = None

    # NEU: AVV Status
    avv_accepted_at: Optional[datetime] = None
    avv_version: Optional[str] = None

class SubscriptionDetails(BaseModel):
    plan: Optional[str] = None
    status: Optional[str] = None
    cancel_at_period_end: bool = False
    current_period_end: Optional[datetime] = None
    next_payment_amount: Optional[float] = None
    next_payment_date: Optional[datetime] = None

class AVVAccept(BaseModel):
    version: str = "1.0"

class SubscriptionUpdate(BaseModel):
    subdomain: str
    plan: str

class TrainingTypeBase(BaseModel):
    name: str
    category: str
    default_price: float = 0.0
    rank_order: int = 0

class TrainingTypeCreate(TrainingTypeBase):
    pass

class TrainingType(TrainingTypeBase):
    id: int
    tenant_id: int
    created_at: datetime
    class Config: from_attributes = True

class LevelRequirementBase(BaseModel):
    training_type_id: int
    required_count: int = 1
    is_additional: bool = False

class LevelRequirementCreate(LevelRequirementBase):
    pass

class LevelRequirement(LevelRequirementBase):
    id: int
    level_id: int
    training_type: Optional[TrainingType] = None 
    class Config: from_attributes = True

class LevelBase(BaseModel):
    name: str
    rank_order: int
    icon_url: Optional[str] = None
    has_additional_requirements: bool = False

class LevelCreate(LevelBase):
    requirements: List[LevelRequirementCreate] = []

class Level(LevelBase):
    id: int
    tenant_id: int
    requirements: List[LevelRequirement] = []
    class Config: from_attributes = True

class DogBase(BaseModel):
    name: str
    breed: Optional[str] = None
    birth_date: Optional[date] = None
    chip: Optional[str] = None

class DogCreate(DogBase):
    pass

class Dog(DogBase):
    id: int
    owner_id: int
    tenant_id: int
    created_at: datetime
    class Config: from_attributes = True

class UserBase(BaseModel):
    email: EmailStr
    name: str
    role: str
    is_active: bool = True
    balance: float = 0.0
    phone: Optional[str] = None
    is_vip: bool = False
    is_expert: bool = False
    current_level_id: Optional[int] = None

class UserCreate(UserBase):
    password: Optional[str] = None 
    auth_id: Optional[UUID] = None
    dogs: List[DogCreate] = []

class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    balance: Optional[float] = None
    phone: Optional[str] = None
    is_vip: Optional[bool] = None
    is_expert: Optional[bool] = None
    current_level_id: Optional[int] = None
    level_id: Optional[int] = None
    password: Optional[str] = None

class UserStatusUpdate(BaseModel):
    is_vip: Optional[bool] = None
    is_expert: Optional[bool] = None

class Document(BaseModel):
    id: int
    tenant_id: int
    user_id: int
    file_name: str
    file_type: str
    upload_date: datetime
    file_path: str
    class Config: from_attributes = True

class AchievementBase(BaseModel):
    training_type_id: int
    date_achieved: datetime
    is_consumed: bool = False

class Achievement(AchievementBase):
    id: int
    tenant_id: int
    user_id: int
    transaction_id: Optional[int] = None
    training_type: Optional[TrainingType] = None
    class Config: from_attributes = True

class User(UserBase):
    id: int
    tenant_id: int
    auth_id: Optional[UUID] = None
    customer_since: datetime
    dogs: List[Dog] = []
    current_level: Optional[Level] = None
    level_id: Optional[int] = None
    documents: List[Document] = []
    achievements: List[Achievement] = []
    class Config: from_attributes = True

class TransactionBase(BaseModel):
    type: str 
    description: Optional[str] = None
    amount: float

class TransactionCreate(TransactionBase):
    user_id: int
    training_type_id: Optional[int] = None 

class Transaction(TransactionBase):
    id: int
    tenant_id: int
    user_id: int
    booked_by_id: int
    balance_after: float
    date: datetime
    bonus: float = 0.0
    class Config: from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str
    user: User

class TokenData(BaseModel):
    email: Optional[str] = None
    tenant_id: Optional[int] = None 

class UserLevelUpdate(BaseModel):
    level_id: int

class UserVipUpdate(BaseModel):
    is_vip: bool

class UserExpertUpdate(BaseModel):
    is_expert: bool

class BookingBase(BaseModel):
    pass

class BookingCreate(BookingBase):
    appointment_id: int

class Booking(BookingBase):
    id: int
    tenant_id: int
    appointment_id: int
    user_id: int
    status: str
    attended: bool
    created_at: datetime
    user: Optional[User] = None
    class Config: from_attributes = True

class AppointmentBase(BaseModel):
    title: str
    description: Optional[str] = None
    start_time: datetime
    end_time: datetime
    location: Optional[str] = None
    max_participants: int = 10
    trainer_id: Optional[int] = None
    target_level_ids: List[int] = []

class AppointmentCreate(AppointmentBase):
    pass

class AppointmentUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    max_participants: Optional[int] = None
    trainer_id: Optional[int] = None
    target_level_ids: Optional[List[int]] = None

class Appointment(AppointmentBase):
    id: int
    tenant_id: int
    created_at: datetime
    bookings: List[Booking] = []
    participants_count: Optional[int] = None 
    trainer: Optional[User] = None
    target_levels: List[Level] = []
    class Config: from_attributes = True

class AppConfig(BaseModel):
    tenant: Tenant
    levels: List[Level]
    training_types: List[TrainingType]
    appointments: List[Appointment] = []

class ServiceUpdateItem(BaseModel):
    id: Optional[int] = None
    name: str
    category: str
    price: float
    rank_order: int = 0

class TopUpOption(BaseModel):
    amount: float
    bonus: float = 0.0

class RequirementUpdateItem(BaseModel):
    id: Optional[int] = None
    training_type_id: int
    required_count: int
    is_additional: bool = False

class LevelUpdateItem(BaseModel):
    id: Optional[int] = None
    name: str
    rank_order: int
    badge_image: Optional[str] = None
    has_additional_requirements: bool = False
    requirements: List[RequirementUpdateItem] = []

class SettingsUpdate(BaseModel):
    school_name: str
    support_email: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: str
    secondary_color: str
    background_color: str
    sidebar_color: str
    level_term: str
    vip_term: str
    allow_custom_top_up: bool = True
    top_up_options: List[TopUpOption] = []
    services: List[ServiceUpdateItem]
    levels: List[LevelUpdateItem]
    active_modules: List[str] = []

class NewsletterSubscriberBase(BaseModel):
    email: EmailStr
    source: Optional[str] = "marketing_site"

class NewsletterSubscriberCreate(NewsletterSubscriberBase):
    pass

class NewsletterSubscriber(NewsletterSubscriberBase):
    id: int
    is_subscribed: bool
    created_at: datetime
    class Config: from_attributes = True

class NewsPostBase(BaseModel):
    title: str
    content: str
    image_url: Optional[str] = None
    target_level_ids: List[int] = []
    target_appointment_ids: List[int] = []

class NewsPostCreate(NewsPostBase):
    pass

class NewsPostUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    image_url: Optional[str] = None
    target_level_ids: Optional[List[int]] = None
    target_appointment_ids: Optional[List[int]] = None

class NewsPost(NewsPostBase):
    id: int
    tenant_id: int
    created_by_id: int
    created_at: datetime
    target_level_ids: List[int] = []
    target_appointment_ids: List[int] = []
    author_name: Optional[str] = None
    class Config: from_attributes = True

class ChatMessageBase(BaseModel):
    content: str
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    file_name: Optional[str] = None

class ChatMessageCreate(ChatMessageBase):
    receiver_id: int

class ChatMessage(ChatMessageBase):
    id: int
    tenant_id: int
    sender_id: int
    receiver_id: int
    is_read: bool
    created_at: datetime
    class Config: from_attributes = True

class ChatConversation(BaseModel):
    user: User 
    last_message: Optional[ChatMessage] = None
    unread_count: int = 0

class Invoice(BaseModel):
    id: str
    number: Optional[str]
    created: datetime
    amount: float
    status: str
    pdf_url: Optional[str]
    hosted_url: Optional[str]

class AppStatusBase(BaseModel):
    status: str
    message: Optional[str] = None

class AppStatusUpdate(AppStatusBase):
    pass

class AppStatus(AppStatusBase):
    id: int
    updated_at: datetime
    class Config: from_attributes = True

class PushSubscriptionCreate(BaseModel):
    endpoint: str
    keys: Dict[str, str] # Erwartet keys.p256dh und keys.auth