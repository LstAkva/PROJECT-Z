from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from database import engine

app = FastAPI()

# Говорим FastAPI, где лежат наши HTML файлы
templates = Jinja2Templates(directory="templates")

@app.get("/health")
def health_check():
    """Эндпоинт для проверки здоровья сервера. Render будет использовать его, чтобы понять, жив ли сайт."""
    db_status = "configured" if engine else "not configured"
    return {"status": "ok", "database": db_status}

@app.get("/")
def read_root(request: Request):
    """Главная страница."""
    return templates.TemplateResponse(request=request, name="index.html")