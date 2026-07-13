from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

CHATBOT_DATABASE_SCHEMA = "chatbot"


class ChatbotBase(DeclarativeBase):
    """chatbot専用DBのテーブル定義の基底クラス。"""

    metadata = MetaData(schema=CHATBOT_DATABASE_SCHEMA)
