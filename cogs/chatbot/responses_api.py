import datetime
import os
from logging import getLogger
from typing import Any

import dateutil
from discord import Message
from openai import AsyncOpenAI

from .prompt import draft_generator_prompt, response_styler_prompt

logger = getLogger(__name__)

OPENAI_MODEL = os.getenv("RESPONSES_API_MODEL", "gpt-5.2")
LOCAL_TIMEZONE = dateutil.tz.gettz("Asia/Tokyo")


def convert_message_to_chatgpt_input(message: Message) -> list[dict[str, Any]]:
    chatgpt_input: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": message.clean_content,
                }
            ],
        }
    ]

    for attachment in message.attachments:
        if attachment.content_type in ("image/jpeg", "image/png"):
            # OpenAI supports PNG (.png), JPEG (.jpeg, .jpg), WEBP (.webp), and Non-animated GIF (.gif).
            # Files with uncommon extensions (e.g., .jfif) may cause errors.
            # see https://platform.openai.com/docs/guides/images-vision

            chatgpt_input[0]["content"].append({"type": "input_image", "image_url": attachment.url})

        if attachment.content_type == "application/pdf":
            chatgpt_input[0]["content"].append({"type": "input_file", "file_url": attachment.url})

    return chatgpt_input


class DraftGenerator:
    def __init__(self, client: AsyncOpenAI) -> None:
        self.client = client

    async def draft(self, ai_id: int, short_term_memory: list[Message]) -> str:
        api_response = await self.client.responses.create(
            input=self.generate_prompt(ai_id, short_term_memory),
            model=OPENAI_MODEL,
            reasoning={"effort": "medium"},
            tools=[
                {
                    "type": "web_search",
                    "user_location": {"type": "approximate", "country": "JP"},
                },
                {
                    "type": "code_interpreter",
                    "container": {"type": "auto"},
                },
            ],
        )

        logger.debug("API called: %s", api_response)
        logger.debug("Draft: %s", api_response.output[-1].content[0].text)  # type: ignore
        return api_response.output[-1].content[0].text  # type: ignore

    def generate_prompt(self, ai_id: int, short_term_memory: list[Message]) -> str:
        message_chain = ""

        for m in short_term_memory:
            if m.author.id == ai_id:
                message_chain += f"- Papyrus ({m.created_at.astimezone(LOCAL_TIMEZONE)}) '{m.clean_content}'\n"
            else:
                message_chain += f"- User@{m.author.id} ({m.created_at.astimezone(LOCAL_TIMEZONE)}) '{m.clean_content}'\n"
        message_chain += f"- Papyrus ({datetime.datetime.now(LOCAL_TIMEZONE)}) '●●'"

        return draft_generator_prompt.DRAFT_PROMPT.format(message_chain)


class ResponseStyler:
    def __init__(self, client: AsyncOpenAI) -> None:
        self.client = client

    async def style(self, original_draft: str) -> str:
        api_response = await self.client.responses.create(
            input=self.generate_prompt(original_draft),
            model=OPENAI_MODEL,
        )
        logger.debug("Styled output: %s", api_response.output[-1].content[0].text)  # type: ignore
        return api_response.output[-1].content[0].text  # type: ignore

    def generate_prompt(self, original_draft: str) -> str:
        return response_styler_prompt.STYLE_PROMPT.format(original_draft)
