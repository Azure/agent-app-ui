"""Failure diagnostics without dependency messages, locals or exception chains."""

import logging
import sys
from pathlib import Path


def log_boundary_failure(
    logger: logging.Logger,
    message: str,
    *args: object,
    level: int = logging.ERROR,
) -> None:
    error = sys.exception()
    error_type = type(error).__name__ if error is not None else "unknown"
    location = "unknown"
    if error is not None:
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            location = (
                f"{Path(frame.f_code.co_filename).name}:"
                f"{traceback.tb_lineno}:{frame.f_code.co_name}"
            )
            traceback = traceback.tb_next
    logger.log(
        level,
        message + " [exception_type=%s failure_site=%s]",
        *args,
        error_type,
        location,
        stacklevel=2,
    )
