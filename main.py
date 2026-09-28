from fastapi import FastAPI, Request, Depends, status, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import os
from database import test_database_connection, get_db
from api.quizzes import router as quizzes_router
from api.play import router as play_router
from api.questions import router as questions_router
from api.drafts import router as drafts_router
from api.owner import router as owner_router, get_owner_emails
from contextlib import asynccontextmanager
from api.auth import router as auth_router, get_secret_key, get_current_user_optional


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validates SECRET_KEY configuration (fails loudly in production if missing)
    get_secret_key()
    yield


app = FastAPI(title="ZakoWhat API", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(quizzes_router)
app.include_router(play_router)
app.include_router(questions_router)
app.include_router(drafts_router)
app.include_router(owner_router)
app.include_router(auth_router)

@app.get("/health")
def health_check():
    db_is_connected = test_database_connection()
    return {
        "status": "ok",
        "database": "connected" if db_is_connected else "unavailable"
    }

@app.get("/")
def read_grand_sanctum_homepage(request: Request):
    return templates.TemplateResponse(request=request, name="homepage_grand_sanctum.html")

@app.get("/arena")
@app.get("/quiz/{quiz_id}")
@app.get("/play/{quiz_id}")
@app.get("/staging")
@app.get("/staging/{quiz_id}")
def read_root(request: Request, quiz_id: int = None):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/bank")
@app.get("/admin")
def read_bank(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    owner_emails = get_owner_emails()
    if not user or not owner_emails or user.email.lower() not in owner_emails:
        return RedirectResponse(url="/owner", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="bank.html")

@app.get("/builder")
@app.get("/builder/{draft_id}")
@app.get("/create")
def read_builder(request: Request, draft_id: int = None, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    owner_emails = get_owner_emails()
    if not user or not owner_emails or user.email.lower() not in owner_emails:
        return RedirectResponse(url="/owner", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="builder.html")

@app.get("/owner")
@app.get("/owner/{subpath:path}")
def read_owner_page(request: Request, subpath: str = ""):
    return templates.TemplateResponse(request=request, name="owner.html")

@app.get("/preview/grand-sanctum")
@app.get("/preview/homepage")
def read_preview_grand_sanctum(request: Request):
    return templates.TemplateResponse(request=request, name="homepage_grand_sanctum.html")


