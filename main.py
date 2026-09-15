from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from database import test_database_connection

app = FastAPI()
templates = Jinja2Templates(directory="templates")

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