# app/schemas.py
from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional, Dict, Any
from datetime import datetime, date
from uuid import UUID

class InvoiceSettings(BaseModel):
    company_name: Optional[str] = None
    address_line1: Optional[str] = None # Strasse Hausnr
    address_line2: Optional[str] = None # PLZ Stadt
    tax_number: Optional[str] = None
    vat_id: Optional[str] = None
    registry_court: Optional[str] = None
    registry_number: Optional[str] = None
    bank_name: Optional[str] = None
    iban: Optional[str] = None
    bic: Optional[str] = None
    account_holder: Optional[str] = None
    footer_text: Optional[str] = None
    logo_url: Optional[str] = None # Optional override
    vat_rate: float = 19.0
    is_small_business: bool = False
    small_business_text: Optional[str] = "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet."
    owner_name: Optional[str] = None
    fantasie_name: Optional[str] = None

class WidgetSettings(BaseModel):
    type: str = "status"
    theme: Optional[str] = "light"
    primary_color: Optional[str] = "f97316"
    layout: str = "compact"
    limit: int = 5
    height: int = 200

class LegalSettings(BaseModel):
    company_name: Optional[str] = ""
    legal_form: str = "individual"
    owner_name: Optional[str] = ""
    representative: Optional[str] = ""
    registry_court: Optional[str] = ""
    registry_number: Optional[str] = ""
    street: Optional[str] = ""
    house_number: Optional[str] = ""
    zip_code: Optional[str] = ""
    city: Optional[str] = ""
    email_public: Optional[str] = ""
    email_support: Optional[str] = ""
    phone: Optional[str] = ""
    supervisory_authority: Optional[str] = ""
    has_vat_id: bool = False
    vat_id: Optional[str] = ""
    separate_billing_address: bool = False
    billing_company_name: Optional[str] = ""
    billing_street: Optional[str] = ""
    billing_house_number: Optional[str] = ""
    billing_zip_code: Optional[str] = ""
    billing_city: Optional[str] = ""

class TenantConfig(BaseModel):
    branding: Dict[str, Any] = {} 
    wording: Dict[str, str] = {} 
    balance: Dict[str, Any] = {} 
    features: Dict[str, bool] = {} 
    active_modules: List[str] = ["news", "documents"]
    active_addons: List[str] = [] # NEU: Dynamisch aus der DB befüllt
    upcoming_plan: Optional[str] = None # NEU: Für Vorausschau
    auto_billing_enabled: bool = False
    auto_progress_enabled: bool = False
    appointments: Dict[str, Any] = {"default_duration": 60, "max_participants": 10}
    invoice_settings: Optional[InvoiceSettings] = None
    widgets: Optional[WidgetSettings] = None
    legal_settings: Optional[LegalSettings] = None

class TenantBase(BaseModel):
    name: str
    subdomain: str
    support_email: Optional[str] = None
    plan: Optional[str] = "starter"
    is_active: bool = True
    config: TenantConfig = TenantConfig()

    # Adressdaten (flach in DB, für Checkout etc.)
    street: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    country: Optional[str] = None
    vat_id: Optional[str] = None

class TenantCreate(TenantBase):
    pass

class Tenant(TenantBase):
    id: int
    created_at: datetime
    subscription_ends_at: Optional[datetime] = None
    
    # NEU: Felder auch im Tenant Schema
    stripe_subscription_status: Optional[str] = None
    cancel_at_period_end: bool = False
    active_addons: List[str] = [] # NEU: Explizit hier statt nur in config
    upcoming_plan: Optional[str] = None # NEU: Explizit hier
    # NEU:
    avv_accepted_at: Optional[datetime] = None
    avv_accepted_version: Optional[str] = None
    top_up_fee_percent: float = 0.0

    class Config:
        from_attributes = True

class TenantStatus(BaseModel):
    exists: bool
    tenant_id: Optional[int] = None
    name: Optional[str] = None
    subscription_valid: bool = False
    subscription_ends_at: Optional[datetime] = None
    plan: Optional[str] = None
    has_payment_method: bool = False 
    in_trial: bool = False
    
    # NEU: WICHTIG - Diese Felder müssen hier rein!
    stripe_subscription_id: Optional[str] = None
    stripe_subscription_status: Optional[str] = None
    cancel_at_period_end: bool = False
    
    # NEU: Vorschau-Daten
    next_payment_amount: Optional[float] = None
    next_payment_date: Optional[datetime] = None
    upcoming_plan: Optional[str] = None
    upcoming_addons: Optional[List[str]] = None
    cancelled_addons: Optional[List[str]] = None

    # NEU: AVV Status
    avv_accepted_at: Optional[datetime] = None
    avv_version: Optional[str] = None
    
    # NEU: Zusätzliche Daten für den AVV
    tenant_address: Optional[str] = None
    current_avv_version: str = "1.0"
    
    # NEU: Usage & Limits
    customer_count: int = 0
    max_customers: int = 0
    additional_cost_per_customer: float = 0.0
    top_up_fee_percent: float = 0.0
    current_billing_period_fees: float = 0.0
    active_addons: List[str] = []
    
    # Abwärtskompatibilität
    config: Dict[str, Any] = {}

# --- 1b. ABOS & PAKETE ---
class SubscriptionPackageBase(BaseModel):
    plan_name: str
    package_type: str = "base" # 'base' oder 'addon'
    price_monthly: float = 0.0
    price_yearly: float = 0.0
    allowed_modules: List[str] = ["news", "documents"]
    included_customers: int = 0
    top_up_fee_percent: float = 0.0
    features: Dict[str, bool] = {}
    additional_cost_per_customer: float = 0.0
    
    # Stripe IDs (optional bei Create, werden vom System gesetzt)
    stripe_product_id: Optional[str] = None
    stripe_price_id_base_monthly: Optional[str] = None
    stripe_price_id_base_yearly: Optional[str] = None
    stripe_price_id_users: Optional[str] = None
    stripe_price_id_fees: Optional[str] = None

class SubscriptionPackageCreate(SubscriptionPackageBase):
    pass

class SubscriptionPackage(SubscriptionPackageBase):
    id: int

    class Config:
        from_attributes = True

class SuperAdminStats(BaseModel):
    total_tenants: int
    active_tenants: int
    total_revenue: float
    total_users: int
    new_tenants_last_month: int

class SubscriptionDetails(BaseModel):
    plan: Optional[str] = None
    status: Optional[str] = None
    cancel_at_period_end: bool = False
    current_period_end: Optional[datetime] = None
    next_payment_amount: Optional[float] = None
    next_payment_date: Optional[datetime] = None

class AVVAccept(BaseModel):
    version: str = "1.0"

class BillingDetails(BaseModel):
    company_name: Optional[str] = None
    name: str
    address_line1: str
    postal_code: str
    city: str
    country: str = "DE"
    vat_id: Optional[str] = None

class SubscriptionUpdate(BaseModel):
    subdomain: str
    plan: str
    cycle: str = "monthly"
    addons: List[str] = []
    billing_details: Optional[BillingDetails] = None
    trial_allowed: bool = True

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
    color: Optional[str] = None
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
    image_url: Optional[str] = None
    current_level_id: Optional[int] = None

class DogCreate(DogBase):
    current_level_id: Optional[int] = None

class Dog(DogBase):
    id: int
    owner_id: int
    tenant_id: int
    current_level_id: Optional[int] = None
    current_level: Optional["Level"] = None
    created_at: datetime
    class Config: from_attributes = True

class UserBase(BaseModel):
    email: EmailStr
    name: str
    vorname: Optional[str] = None
    nachname: Optional[str] = None
    role: str
    is_active: bool = True
    balance: float = 0.0
    phone: Optional[str] = None
    is_vip: bool = False
    is_expert: bool = False
    current_level_id: Optional[int] = None
    is_superadmin: bool = False
    notifications_push: bool = False
    
    notif_email_overall: bool = True
    notif_email_chat: bool = True
    notif_email_news: bool = True
    notif_email_booking: bool = True
    notif_email_reminder: bool = True
    notif_email_alert: bool = True
    
    notif_push_overall: bool = False
    notif_push_chat: bool = False
    notif_push_news: bool = False
    notif_push_booking: bool = False
    notif_push_reminder: bool = False
    notif_push_alert: bool = False
    
    reminder_offset_minutes: int = 60
    ical_token: Optional[str] = None
    permissions: Dict[str, bool] = {
        "can_create_courses": False,
        "can_edit_status": False,
        "can_delete_customers": False,
        "can_edit_customers": False,
        "can_create_messages": False
    }

class UserCreate(UserBase):
    password: Optional[str] = None 
    auth_id: Optional[UUID] = None
    dogs: List[DogCreate] = []

class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    name: Optional[str] = None
    vorname: Optional[str] = None
    nachname: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    balance: Optional[float] = None
    phone: Optional[str] = None
    is_vip: Optional[bool] = None
    is_expert: Optional[bool] = None
    current_level_id: Optional[int] = None
    level_id: Optional[int] = None
    is_superadmin: Optional[bool] = None
    notifications_push: Optional[bool] = None
    
    notif_email_overall: Optional[bool] = None
    notif_email_chat: Optional[bool] = None
    notif_email_news: Optional[bool] = None
    notif_email_booking: Optional[bool] = None
    notif_email_reminder: Optional[bool] = None
    notif_email_alert: Optional[bool] = None
    
    notif_push_overall: Optional[bool] = None
    notif_push_chat: Optional[bool] = None
    notif_push_news: Optional[bool] = None
    notif_push_booking: Optional[bool] = None
    notif_push_reminder: Optional[bool] = None
    notif_push_alert: Optional[bool] = None
    
    reminder_offset_minutes: Optional[int] = None
    permissions: Optional[Dict[str, bool]] = None
    
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
    dog_id: Optional[int] = None # NEU
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
    tenant_id: Optional[int] = None
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
    invoice_number: Optional[str] = None

class TransactionCreate(TransactionBase):
    user_id: Any
    dog_id: Optional[int] = None # NEU
    training_type_id: Optional[int] = None 
    top_up_fee: Optional[float] = None

class Transaction(TransactionBase):
    id: int
    tenant_id: int
    user_id: int
    booked_by_id: Optional[int] = None
    balance_after: float
    date: datetime
    bonus: float = 0.0
    top_up_fee: float = 0.0
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
    dog_id: Optional[int] = None # NEU
    is_billed: bool = False # NEU

class AppointmentBase(BaseModel):
    title: str
    description: Optional[str] = None
    start_time: datetime
    end_time: datetime
    location: Optional[str] = None
    max_participants: int = 10
    trainer_id: Optional[int] = None
    target_level_ids: List[int] = []
    training_type_id: Optional[int] = None
    price: Optional[float] = None # NEU
    is_open_for_all: bool = False
    block_id: Optional[str] = None # NEU: Block-Kurs ID

class AppointmentShort(AppointmentBase):
    id: int
    tenant_id: int
    created_at: datetime
    participants_count: Optional[int] = None 
    trainer: Optional[User] = None
    training_type: Optional[TrainingType] = None
    target_levels: List[Level] = []
    class Config: from_attributes = True

class Booking(BookingBase):
    id: int
    tenant_id: int
    appointment_id: int
    user_id: int
    status: str
    attended: bool
    is_billed: bool = False # NEU
    dog_id: Optional[int] = None # NEU
    created_at: datetime
    user: Optional[User] = None
    dog: Optional[Dog] = None
    appointment: Optional[AppointmentShort] = None # NEU: Damit Termin-Details im UI sichtbar sind
    warning: Optional[str] = None # NEU: Für Guthaben-Warnungen
    class Config: from_attributes = True

class AppointmentCreate(AppointmentBase):
    pass

class AppointmentRecurringCreate(AppointmentCreate):
    recurrence_pattern: str # daily, weekly, biweekly, weekdays
    end_after_count: Optional[int] = None
    end_at_date: Optional[datetime] = None
    is_block: bool = False # NEU: Markiert als geschlossener Kurs

class AppointmentUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    max_participants: Optional[int] = None
    trainer_id: Optional[int] = None
    target_level_ids: Optional[List[int]] = None
    training_type_id: Optional[int] = None
    price: Optional[float] = None # NEU
    is_open_for_all: Optional[bool] = None
    block_id: Optional[str] = None

class Appointment(AppointmentBase):
    id: int
    tenant_id: int
    created_at: datetime
    bookings: List[Booking] = []
    participants_count: Optional[int] = None 
    trainer: Optional[User] = None
    training_type: Optional[TrainingType] = None
    target_levels: List[Level] = []
    class Config: from_attributes = True

class AppConfig(BaseModel):
    tenant: Tenant
    levels: List[Level]
    training_types: List[TrainingType]
    appointments: List[Appointment] = []
    active_addons: List[str] = [] # NEU
    upcoming_plan: Optional[str] = None # NEU

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
    color: Optional[str] = None
    has_additional_requirements: bool = False
    requirements: List[RequirementUpdateItem] = []
    
class AppointmentSettings(BaseModel):
    default_duration: int = 60
    max_participants: int = 10
    cancelation_period_hours: int = 0
    color_rules: List[Dict[str, Any]] = []
    locations: List[Dict[str, Any]] = []

class SettingsUpdate(BaseModel):
    school_name: str
    support_email: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: str
    secondary_color: str
    background_color: str
    sidebar_color: str
    open_for_all_color: Optional[str] = None
    workshop_lecture_color: Optional[str] = None
    level_term: str
    vip_term: str
    allow_custom_top_up: bool = True
    top_up_options: List[TopUpOption] = []
    services: List[ServiceUpdateItem]
    levels: List[LevelUpdateItem]
    active_modules: List[str] = []
    auto_billing_enabled: bool = False
    auto_progress_enabled: bool = False
    appointments: Optional[AppointmentSettings] = None
    invoice_settings: Optional[InvoiceSettings] = None
    legal_settings: Optional[LegalSettings] = None
    widgets: Optional[WidgetSettings] = None

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

class TopUpIntentCreate(BaseModel):
    amount: float
    bonus: float

class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    subdomain: str

class PasswordReset(BaseModel):
    password: str


# --- HAUSAUFGABEN ---

class ExerciseTemplateBase(BaseModel):
    title: str
    description: Optional[str] = None
    video_url: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    attachments: List[Dict[str, Any]] = []

class ExerciseTemplateCreate(ExerciseTemplateBase):
    pass

class ExerciseTemplateUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    video_url: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = None

class ExerciseTemplate(ExerciseTemplateBase):
    id: int
    tenant_id: int
    created_at: datetime
    class Config: from_attributes = True

class HomeworkAssignmentBase(BaseModel):
    user_id: int
    dog_id: Optional[int] = None
    template_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    video_url: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    attachments: List[Dict[str, Any]] = []

class HomeworkAssignmentCreate(HomeworkAssignmentBase):
    pass

class HomeworkAssignmentUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    video_url: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = None
    is_completed: Optional[bool] = None
    client_feedback: Optional[str] = None

class HomeworkCompletionRequest(BaseModel):
    client_feedback: Optional[str] = None

class HomeworkAssignment(HomeworkAssignmentBase):
    id: int
    tenant_id: int
    assigned_by_id: Optional[int] = None
    is_completed: bool
    completed_at: Optional[datetime] = None
    client_feedback: Optional[str] = None
    created_at: datetime
    class Config: from_attributes = True


# --- TEILNAHMEBESCHEINIGUNGEN ---

class CertificateTemplateBase(BaseModel):
    name: str
    layout_id: str
    images: Dict[str, str] = {} # Flexibler Speicher für Bilder {"logo": "url", ...}
    title: str = "Teilnahmebescheinigung"
    body_text: Optional[str] = None
    trigger_type: str
    target_id: int
    preview_data: Optional[Dict[str, str]] = None # NEU: Testdaten für die Vorschau

class CertificateTemplateCreate(CertificateTemplateBase):
    pass

class CertificateTemplateUpdate(BaseModel):
    name: Optional[str] = None
    layout_id: Optional[str] = None
    images: Optional[Dict[str, str]] = None
    title: Optional[str] = None
    body_text: Optional[str] = None
    trigger_type: Optional[str] = None
    target_id: Optional[int] = None
    preview_data: Optional[Dict[str, str]] = None

class CertificateTemplateResponse(CertificateTemplateBase):
    id: int
    tenant_id: int
    created_at: datetime
    class Config: from_attributes = True

class CertificateLayoutMetadata(BaseModel):
    id: str
    name: str
    image_slots: List[Dict[str, Any]]
    placeholders: List[str]
    trigger_data: Dict[str, Dict[str, Any]] = {}