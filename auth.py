import os
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext

SECRET_KEY  = os.getenv("SECRET_KEY", "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION_ENV")
ALGORITHM   = "HS256"
TOKEN_EXPIRE_HOURS = 72

# Admin credentials — set via environment variables on Railway
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "xray1234")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/admin/login")

def create_token(data: dict):
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({**data, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username != ADMIN_USER:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Token expired or invalid")
