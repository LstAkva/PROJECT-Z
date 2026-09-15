import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Достаем ссылку на базу данных из скрытых настроек
DATABASE_URL = os.getenv("DATABASE_URL")

# Если ссылка есть, настраиваем подключение. Если нет (пока мы тестируем без нее) - не падаем с ошибкой.
if DATABASE_URL:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
else:
    engine = None
    SessionLocal = None

Base = declarative_base()