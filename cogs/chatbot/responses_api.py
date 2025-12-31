import os
from logging import getLogger
from typing import Any, Optional

from discord import Message
from openai import AsyncOpenAI

from .prompt import INSTRUCTIONS

logger = getLogger(__name__)

OPENAI_MODEL = os.getenv("RESPONSES_API_MODEL", "gpt-4.1")


def convert_message_to_chatgpt_input(message: Message):
    input: list[dict[str, Any]] = [
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

            input[0]["content"].append(
                {"type": "input_image", "image_url": attachment.url}
            )

        if attachment.content_type == "application/pdf":
            input[0]["content"].append(
                {"type": "input_file", "file_url": attachment.url}
            )

    return input


async def fetch_chatgpt_output_text(
    client: AsyncOpenAI, message: Message, previous_response_id: Optional[str] = None
) -> tuple[str, str]:
    response = await client.responses.create(
        input=convert_message_to_chatgpt_input(message),  # type: ignore
        instructions=INSTRUCTIONS,
        model=OPENAI_MODEL,
        previous_response_id=previous_response_id,
        temperature=0.9,
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

    logger.info(f"{response=}")

    return response.output[-1].content[0].text, response.id  # type: ignore
