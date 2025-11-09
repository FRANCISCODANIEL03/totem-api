from fastapi import FastAPI, Depends
from starlette.middleware.sessions import SessionMiddleware
from app import models
from app.db import engine, Base
from app.auth import router as auth_router
from app.templates_routes import router as templates_router
from app.schemas import UserOut
from app.utils import get_current_user
from app.config import SECRET_KEY
from tabulate import tabulate
import uvicorn

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Auth API")

app.include_router(auth_router)
app.include_router(templates_router)

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
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
