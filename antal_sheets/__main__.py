import argparse
import logging

from .bot import SheetBot
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Discord roles to Google groups")
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    if config.dry_run:
        logging.getLogger(__name__).warning("DRY RUN: changes are logged, not applied")

    bot = SheetBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
