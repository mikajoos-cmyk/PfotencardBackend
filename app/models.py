from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Date, Boolean
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False)
    is_active = Column(Boolean, default=True)
    balance = Column(Float, default=0.0)
    customer_since = Column(DateTime, server_default=func.now())
    phone = Column(String(50), nullable=True)
    level_id = Column(Integer, default=1, nullable=False)
    is_vip = Column(Boolean, default=False, nullable=False)  # <-- NEUE ZEILE
    is_expert = Column(Boolean, default=False, nullable=False)
    dogs = relationship("Dog", back_populates="owner", cascade="all, delete-orphan")
    # HIER DIE KORREKTUR: Wir sagen SQLAlchemy, welche Spalte es für diese Beziehung nutzen soll.
    transactions = relationship("Transaction", foreign_keys='[Transaction.user_id]', back_populates="user", cascade="all, delete-orphan")
    achievements = relationship("Achievement", back_populates="user", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="user", cascade="all, delete-orphan")


class Dog(Base):
    __tablename__ = 'dogs'
    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    name = Column(String(255), index=True, nullable=False)
    breed = Column(String(255))
    birth_date = Column(Date)
    chip = Column(String(50), nullable=True)
    owner = relationship("User", back_populates="dogs")


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    date = Column(DateTime, server_default=func.now())
    type = Column(String(255), nullable=False)
    description = Column(String(255))
    amount = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    booked_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)

    # HIER DIE KORREKTUR: Wir weisen jede Beziehung explizit einer Fremdschlüssel-Spalte zu.
    user = relationship("User", foreign_keys=[user_id], back_populates="transactions")
    booked_by = relationship("User", foreign_keys=[booked_by_id])


class Achievement(Base):
    __tablename__ = 'achievements'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    requirement_id = Column(String(255), nullable=False)
    date_achieved = Column(DateTime, server_default=func.now())
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True)
    is_consumed = Column(Boolean, default=False, nullable=False)  # <-- NEUE ZEILE

    user = relationship("User", back_populates="achievements")

class Document(Base):
    __tablename__ = 'documents'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(100), nullable=False)
    upload_date = Column(DateTime, server_default=func.now())
    file_path = Column(String(512), nullable=False)

    user = relationship("User", back_populates="documents")
