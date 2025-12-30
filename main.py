import json
import os
from logging import config, getLogger

import dotenv


def main():
    dotenv.load_dotenv()

    with open("./log/logging_conf.json", "r", encoding="utf-8") as f:
        config.dictConfig(json.load(f))

    logger = getLogger(__name__)
    logger.setLevel(os.getenv("LOG_LEVEL", "WARNING"))

    logger.warning("This is a public service announcement!")
    logger.debug("Debugging!")


if __name__ == "__main__":
    main()
