from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from app.db import get_db
from app import models, schemas
from app.utils import hash_password, verify_password, create_access_token, create_refresh_token, decode_token
from app.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
from authlib.integrations.starlette_client import OAuth, OAuthError
import uuid
import os

router = APIRouter(prefix="/auth", tags=["auth"])

oauth = OAuth()
CONF_URL = 'https://accounts.google.com/.well-known/openid-configuration'
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url=CONF_URL,
    client_kwargs={'scope': 'openid email profile'}
)

# Register user (email+password)
@router.post("/register", response_model=schemas.UserOut)
def register(user_in: schemas.UserCreate, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == user_in.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user = models.User(
        id=str(uuid.uuid4()),
        email=user_in.email,
        hashed_password=hash_password(user_in.password),
        full_name=user_in.full_name,
        is_google=False
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

# Login with email/password
@router.post("/login", response_model=schemas.Token)
def login(form_data: schemas.UserCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == form_data.email).first()
    if not user or not user.hashed_password or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    access = create_access_token({"sub": str(user.id), "email": user.email})
    refresh = create_refresh_token({"sub": str(user.id), "email": user.email})
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}

# Refresh tokens
@router.post("/refresh", response_model=schemas.Token)
def refresh_token(payload: dict, db: Session = Depends(get_db)):
    # Expecting JSON body: {"refresh_token": "..."}
    token = payload.get("refresh_token")
    if not token:
        raise HTTPException(status_code=400, detail="refresh_token required")
    data = decode_token(token)
    if not data or data.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = str(data.get("sub"))
    user = db.query(models.User).filter(models.User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    access = create_access_token({"sub": str(user.id), "email": user.email})
    refresh = create_refresh_token({"sub": str(user.id), "email": user.email})
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}

# Google login - redirect to Google
@router.get("/google/login")
async def google_login(request: Request):
    redirect_uri = GOOGLE_REDIRECT_URI
    # Authlib will create the authorization URL
    return await oauth.google.authorize_redirect(request, redirect_uri)

# Google callback - exchange code for tokens and create/find user
@router.get("/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {e}")
    userinfo = token.get("userinfo")
    # Some providers return `userinfo`, others must fetch userinfo:
    if not userinfo:
        userinfo = await oauth.google.parse_id_token(request, token)
    email = userinfo.get("email")
    full_name = userinfo.get("name")
    if not email:
        raise HTTPException(status_code=400, detail="Unable to get email from Google")
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        user = models.User(
            id=str(uuid.uuid4()),
            email=email,
            full_name=full_name,
            is_google=True,
            hashed_password=None
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    # generate tokens
    access = create_access_token({"sub": str(user.id), "email": user.email})
    refresh = create_refresh_token({"sub": str(user.id), "email": user.email})
    # return tokens as JSON (or redirect to frontend with tokens as query params - be careful!)
    return JSONResponse({"access_token": access, "refresh_token": refresh, "token_type": "bearer", "user": {"email": user.email, "full_name": user.full_name, "id": user.id}})
