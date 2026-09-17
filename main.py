from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from database import test_database_connection
from api.quizzes import router as quizzes_router
from api.play import router as play_router
from api.questions import router as questions_router

app = FastAPI(title="ZakoWhat API")
templates = Jinja2Templates(directory="templates")

app.include_router(quizzes_router)
app.include_router(play_router)
app.include_router(questions_router)

@app.get("/health")
def health_check():
    db_is_connected = test_database_connection()
    return {
        "status": "ok",
        "database": "connected" if db_is_connected else "unavailable"
    }

@app.get("/")
def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/bank")
@app.get("/admin")
def read_bank(request: Request):
    return templates.TemplateResponse(request=request, name="bank.html")