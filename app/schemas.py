from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, date


# --- Base and Create Schemas ---

class DogBase(BaseModel):
    name: str
    breed: Optional[str] = None
    birth_date: Optional[date] = None
    chip: Optional[str] = None

class DogCreate(DogBase):
    pass


class UserBase(BaseModel):
    email: str
    name: str
    role: str
    is_active: bool = True
    balance: float = 0.0
    phone: Optional[str] = None
    level_id: int = 1
    is_vip: bool = False
    is_expert: bool = False

class UserCreate(UserBase):
    password: Optional[str] = None
    dogs: List[DogCreate] = []

# KORREKTUR: Alle Felder optional machen, um Datenverlust bei Teil-Updates zu verhindern
class UserUpdate(BaseModel):
    email: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    balance: Optional[float] = None
    phone: Optional[str] = None
    level_id: Optional[int] = None
    is_vip: Optional[bool] = None
    is_expert: Optional[bool] = None
    password: Optional[str] = None

class UserStatusUpdate(BaseModel):
    is_vip: Optional[bool] = None
    is_expert: Optional[bool] = None

class TransactionBase(BaseModel):
    type: str
    description: Optional[str] = None
    amount: float


class TransactionCreate(TransactionBase):
    user_id: int
    requirement_id: Optional[str] = None


# --- Full Schemas with Relationships ---

class Dog(DogBase):
    id: int
    owner_id: int

    class Config:
        from_attributes = True


class Achievement(BaseModel):
    id: int
    requirement_id: str
    date_achieved: datetime
    is_consumed: bool

    class Config:
        from_attributes = True


class Transaction(TransactionBase):
    id: int
    user_id: int
    date: datetime
    balance_after: float
    booked_by_id: int

    class Config:
        from_attributes = True


class Document(BaseModel):
    id: int
    file_name: str
    file_type: str
    upload_date: datetime
    file_path: str

    class Config:
        from_attributes = True


class User(UserBase):
    id: int
    customer_since: datetime
    dogs: List[Dog] = []
    transactions: List[Transaction] = []
    achievements: List[Achievement] = []
    documents: List[Document] = []

    class Config:
        from_attributes = True


# --- For Login ---
class Token(BaseModel):
    access_token: str
    token_type: str
    user: User


class TokenData(BaseModel):
    email: Optional[str] = None

class UserLevelUpdate(BaseModel):
    level_id: int

class UserVipUpdate(BaseModel):
    is_vip: bool

class UserExpertUpdate(BaseModel):
    is_expert: bool