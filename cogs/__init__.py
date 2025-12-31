from logging import NullHandler, getLogger

logger = getLogger(__package__).addHandler(NullHandler())
