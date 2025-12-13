from fastapi import FastAPI, Depends, Request
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.middleware.cors import CORSMiddleware
from app import models
from app.db import engine, Base
from app.auth import router as auth_router
from app.templates_routes import router as templates_router
from app.schemas import UserOut
from app.utils import get_current_user
from app.config import SECRET_KEY
from tabulate import tabulate
import uvicorn
from app.limiter import limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Totem API", version="1.0.0")

# --- CONFIGURACIÓN DEL RATE LIMITER ---
# 1. Poner el limiter en el estado de la app
app.state.limiter = limiter

# 2. Añadir el manejador de excepciones
@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """
    Responde con un 429 (Too Many Requests) cuando se excede el límite.
    """
    return JSONResponse(
        status_code=429,
        content={"detail": f"Límite de peticiones excedido: {exc.detail}"}
    )

app.add_middleware(SlowAPIMiddleware)

app.include_router(auth_router)
app.include_router(templates_router)

app.add_middleware(
    limiter.middleware,
    key_func=lambda request: request.client.host
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Cambia esto a tus dominios permitidos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


@app.on_event("startup")
async def show_routes():
    data = []
    for route in app.routes:
        if hasattr(route, "methods"):
            methods = ",".join(route.methods)
            data.append([methods, route.path])
    print("\n [XXX] API ROUTES:\n")
    print(tabulate(data, headers=["METHODS", "PATH"], tablefmt="fancy_grid"))


@app.get("/users/me", response_model=UserOut)
def read_users_me(current_user: models.User = Depends(get_current_user)):
    return current_user

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=5005, reload=True)
