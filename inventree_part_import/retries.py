import time
from contextlib import contextmanager
import logging

from inventree.api import InvenTreeAPI
from requests.exceptions import HTTPError, Timeout, ConnectionError as RequestsConnectionError

logger = logging.getLogger(__name__)

class retries:
    def __init__(self, n, context_manager, timeout, exponential_backoff=True):
        self.context_manager = context_manager
        self.retries = 0
        self.max_retries = n
        self.base_timeout = timeout
        self.exponential_backoff = exponential_backoff

    def __iter__(self):
        return self

    def __next__(self):
        if self.retries > self.max_retries:
            raise StopIteration

        if self.retries > 0:
            if self.exponential_backoff:
                # Exponential backoff: 1x, 2x, 4x, 8x, etc.
                sleep_time = self.base_timeout * (2 ** (self.retries - 1))
                # Cap at 30 seconds to avoid too long waits
                sleep_time = min(sleep_time, 30.0)
            else:
                sleep_time = self.base_timeout
            
            logger.warning(f"Retrying request in {sleep_time:.1f} seconds (attempt {self.retries + 1}/{self.max_retries + 1})")
            time.sleep(sleep_time)

        if self.retries == self.max_retries:
            self.retries += 1
            return self._dummy_manager()
        else:
            self.retries += 1
            return self.context_manager(self)

    def stop(self):
        self.retries = self.max_retries + 1

    @contextmanager
    def _dummy_manager(self):
        yield

@contextmanager
def catch_timeouts(_retries: retries):
    try:
        yield
        _retries.stop()
    except (Timeout, ConnectionError, RequestsConnectionError) as e:
        logger.warning(f"Network error occurred: {type(e).__name__}: {str(e)}")
        pass
    except HTTPError as e:
        status_code = None
        if e.response is not None:
            status_code = e.response.status_code
        elif e.args:
            status_code = e.args[0].get("status_code")
        
        logger.warning(f"HTTP error occurred: status {status_code}: {str(e)}")
        
        if status_code not in {408, 409, 500, 502, 503, 504}:
            raise e

class retry_timeouts(retries):
    def __init__(self, n=None, context_manager=catch_timeouts):
        from .config import get_config
        config = get_config()
        if n is None:
            n = config["max_retries"]
        super().__init__(n, context_manager, timeout=config["retry_timeout"], exponential_backoff=True)

class RetryInvenTreeAPI(InvenTreeAPI):
    def testServer(self):
        for retry in retry_timeouts():
            with retry:
                return super().testServer()

    def request(self, api_url, **kwargs):
        for retry in retry_timeouts():
            with retry:
                return super().request(api_url, **kwargs)

    def downloadFile(self, url, destination, overwrite=False, params=None, proxies=...):
        for retry in retry_timeouts():
            with retry:
                return super().downloadFile(url, destination, overwrite, params, proxies)
