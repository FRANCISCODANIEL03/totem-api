from sqlalchemy import Column, String, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from app.db import Base

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_google = Column(Boolean, default=False)

    # Relación con templates
    templates = relationship("Template", back_populates="user")


class Template(Base):
    __tablename__ = "templates"

    id = Column(String, primary_key=True, index=True)
    s3_key = Column(String, nullable=False)
    user_id = Column(String, ForeignKey("users.id"))

    user = relationship("User", back_populates="templates")

    is_public = Column(Boolean, default=False, index=True)

    user = relationship("User", back_populates="templates")
    # Relación con TemplateWithImage
    template_with_images = relationship("TemplateWithImage", back_populates="template")


class TemplateWithImage(Base):
    __tablename__ = "templates_with_images"

    id = Column(String, primary_key=True, index=True)
    s3_key = Column(String, nullable=False)
    user_id = Column(String, ForeignKey("users.id"))

    # 🔑 Nueva relación hacia Template
    template_id = Column(String, ForeignKey("templates.id"), nullable=False)

    user = relationship("User")
    template = relationship("Template", back_populates="template_with_images")
