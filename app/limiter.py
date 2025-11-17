from slowapi import Limiter
from slowapi.util import get_remote_address
from app.config import RATE_LIMIT_GLOBAL, REDIS_URL

# Usará la IP del cliente como clave
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=REDIS_URL,

    default_limits=[RATE_LIMIT_GLOBAL]
)
