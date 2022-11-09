import logging
from threading import Event

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.NullHandler())


shutdown_event = Event()  # signals all long-running threads to stop


TOPIC_HEADER = "/rpc/v1/wb-device-manager/"
