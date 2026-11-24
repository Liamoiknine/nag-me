"""Run the server: `uv run python -m nagme`."""

import logging
import os

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    cfg = Config.from_env()
    for name in cfg.missing():
        logging.getLogger("nagme").warning("%s is not set; see .env.example", name)
    uvicorn.run(
        create_app(cfg),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
