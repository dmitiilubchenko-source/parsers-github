"""Сессия официального сервиса через Selenium и Chrome.

Браузер сам открывает портал. Заголовки его успешного запроса к справочнику
комиссий используются только в памяти процесса, не записываются в файлы.
"""

import json
import time
from contextlib import contextmanager
from threading import Lock

import httpx
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options

from .config import TIMEOUT_SECONDS

SERVICE = "http://apps.cikrf.ru/service/ik-inp-service-pbcopy"
ROOT_URL = (
    "http://www.izbirkom.ru/election/587813923/commission/"
    "f0130cef-663a-4c57-b8ff-17054f8fa34e/voting-flow?type=2&report=453"
)
CLASSIFIER_MARKER = "/commissionClassifiers?electionsId=587813923"


def chrome_options(*, performance_log: bool = False) -> Options:
    options = Options()
    options.add_argument("--headless=new")
    if performance_log:
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return options


def _session_headers(driver, timeout: float = 30) -> dict[str, str]:
    requests: dict[str, dict] = {}
    success: set[str] = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
            except (KeyError, ValueError, TypeError):
                continue
            method, params = message.get("method"), message.get("params", {})
            request_id = params.get("requestId")
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                if CLASSIFIER_MARKER in request.get("url", ""):
                    requests[request_id] = request.get("headers", {})
            elif method == "Network.requestWillBeSentExtraInfo" and request_id in requests:
                requests[request_id].update(params.get("headers", {}))
            elif method == "Network.responseReceived":
                response = params.get("response", {})
                if CLASSIFIER_MARKER in response.get("url", "") and response.get("status") == 200:
                    success.add(request_id)
        for request_id in success:
            headers = requests.get(request_id, {})
            lower = {key.lower(): value for key, value in headers.items()}
            if lower.get("x-api-key"):
                return {key: value for key, value in lower.items()
                        if key in {"x-api-key", "x-client-fingerprint", "user-agent", "origin", "referer"}}
        time.sleep(0.5)
    raise RuntimeError("Selenium не дождался успешного запроса справочника комиссий")


def _open_portal_headers(driver) -> dict[str, str]:
    last_error = None
    for attempt in range(3):
        try:
            driver.get_log("performance")  # Убираем события предыдущего открытия страницы.
            driver.get(ROOT_URL)
            return _session_headers(driver)
        except (WebDriverException, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    detail = str(last_error).splitlines()[0] if last_error is not None else "неизвестная ошибка"
    raise RuntimeError(f"Не удалось открыть официальный портал через Selenium: {detail}") from last_error


@contextmanager
def official_client():
    driver = webdriver.Chrome(options=chrome_options(performance_log=True))
    try:
        driver.set_page_load_timeout(30)
        driver.execute_cdp_cmd("Network.enable", {})
        headers = _open_portal_headers(driver)
        with httpx.Client(base_url=SERVICE, headers=headers, timeout=max(TIMEOUT_SECONDS, 25)) as client:
            refresh_lock = Lock()
            client._session_refresh_count = 0

            def refresh_session(observed_key: str | None) -> None:
                with refresh_lock:
                    if observed_key and client.headers.get("x-api-key") != observed_key:
                        return  # Другой поток уже обновил сессию.
                    fresh_headers = _open_portal_headers(driver)
                    for key in ("x-api-key", "x-client-fingerprint", "user-agent", "origin", "referer"):
                        client.headers.pop(key, None)
                    client.headers.update(fresh_headers)
                    client._session_refresh_count += 1

            client._refresh_session = refresh_session
            yield client
    finally:
        driver.quit()


def get_json(client: httpx.Client, path: str, params: dict) -> dict:
    last_error = None
    refreshed = False
    for attempt in range(3):
        try:
            response = client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            last_error = exc
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                if status in (401, 403) and not refreshed:
                    refresh = getattr(client, "_refresh_session", None)
                    if refresh is not None:
                        refresh(exc.request.headers.get("x-api-key"))
                        refreshed = True
                        continue
                if status not in (429, 500, 502, 503, 504):
                    break
            if attempt < 2:
                time.sleep(attempt + 1)
    if isinstance(last_error, httpx.HTTPStatusError):
        reason = f"HTTP {last_error.response.status_code}"
    elif last_error is not None:
        reason = type(last_error).__name__
    else:
        reason = "неизвестная ошибка"
    raise RuntimeError(f"Сервис не ответил ({reason}): {path}, параметры {params}") from last_error
