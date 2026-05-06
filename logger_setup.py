"""
logger_setup.py
Centralized logging for all CC Video Manager services.
Each service gets its own rotating log file in logs/ (7-day retention).
Format: timestamp | level | function | message
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR  = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

_FORMATTER = logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(funcName)-30s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


def get_logger(name: str) -> logging.Logger:
    """
    Returns a logger for the given service name.
    Writes DEBUG+ to logs/<name>.log (daily rotation, 7-day retention).
    Writes INFO+ to stdout.
    Safe to call multiple times — handlers are only attached once.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fh = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, f'{name}.log'),
        when='midnight',
        backupCount=7,
        encoding='utf-8',
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FORMATTER)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(_FORMATTER)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger
