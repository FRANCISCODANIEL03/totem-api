from app import models
from app.db import get_db
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from PIL import Image
from io import BytesIO
from app.config import SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS, GEMINI_API_KEY
import jwt

client = genai.Client(api_key=GEMINI_API_KEY)

security = HTTPBearer()


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    # bcrypt solo acepta hasta 72 bytes
    password = password.encode("utf-8")[:72].decode("utf-8", "ignore")
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_access_token(data: dict, expires_delta: int | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (timedelta(minutes=expires_delta) if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "type": "access"})
    encoded = jwt.encode(to_encode, SECRET_KEY, algorithm="HS256")
    return encoded

def create_refresh_token(data: dict, expires_delta_days: int | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (timedelta(days=expires_delta_days) if expires_delta_days else timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded = jwt.encode(to_encode, SECRET_KEY, algorithm="HS256")
    return encoded

def decode_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except Exception:
        return None

def process_with_gemini(prompt: str, base_image: Image.Image, other_image: Image.Image = None):
    contents = [prompt, base_image]
    if other_image:
        contents.append(other_image)

    response = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=contents,
    )

    image_parts = [
        part.inline_data.data
        for part in response.candidates[0].content.parts
        if part.inline_data
    ]

    if not image_parts:
        raise ValueError("Gemini did not return an image")

    return Image.open(BytesIO(image_parts[0]))

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security), db: Session = Depends(get_db)):
    token = credentials.credentials
    data = decode_token(token)
    if not data or data.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.query(models.User).filter(models.User.id == data.get("sub")).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user
