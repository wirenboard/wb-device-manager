import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.NullHandler())

TOPIC_HEADER = "/rpc/v1/wb-device-manager/"
