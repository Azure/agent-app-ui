"""
Provides dependencies for API calls.
"""
import logging
from fastapi import HTTPException
from gpt_rag_ui.config.appconfig import AppConfigClient

__config: AppConfigClient = None

# Suppress verbose Azure SDK logging by default; troubleshooting logs should focus on our app flow.
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

logger = logging.getLogger("gpt_rag_ui.dependencies")

def get_config(action: str = None) -> AppConfigClient:
    global __config

    if action is not None and action=='refresh':
        logger.info("Refreshing App Configuration client on demand")
        __config = AppConfigClient()
    else:
        __config = __config or AppConfigClient()
        if __config and action is None:
            logger.debug("Using cached App Configuration client")

    return __config

def handle_exception(exception: Exception, status_code: int = 500):
    logger.error("Dependency failure encountered", exc_info=exception)
    raise HTTPException(
        status_code = status_code,
        detail = str(exception)
    ) from exception
