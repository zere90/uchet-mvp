"""Настройки приложения. Берутся из переменных окружения или файла .env."""
import os
from uuid import UUID

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/uchet")

# Пока нет аутентификации (в PRD — вход через OIDC), все действия
# записываются в журнал от имени этого «пользователя-разработчика».
DEV_USER_ID = UUID(os.getenv("DEV_USER_ID", "00000000-0000-0000-0000-00000000ffff"))
