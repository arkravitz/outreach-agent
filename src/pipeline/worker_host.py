"""Wake the durable outbox immediately before starting the supervised consumer."""

from .worker import wake
from huey.bin.huey_consumer import consumer_main


if __name__ == "__main__":
    wake()
    consumer_main()
