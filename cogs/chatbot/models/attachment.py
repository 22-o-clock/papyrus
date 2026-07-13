from pydantic import BaseModel


class AttachmentAnalysis(BaseModel):
    """短期文脈に保存する添付ファイルの要約です。"""

    summary: str
    important_text: str
