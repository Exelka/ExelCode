"""Анонимный клиент Gemini (веб-протокол gemini.google.com).

Без API-ключа, cookies и сторонних библиотек. Google отдаёт страницу /app
анонимным пользователям; из неё берётся номер сборки (cfb2h), а ответ
приходит потоком кадров wrb.fr от StreamGenerate.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)

PAGE_URL = "https://gemini.google.com/app"
RPC_URL = (
    "https://gemini.google.com/_/BardChatUi/data/"
    "assistant.lamda.BardFrontendService/StreamGenerate"
)

# hex-идентификаторы моделей из веб-клиента. Анонимно Google всё равно
# отдаёт 3.5 Flash-Lite, но маршруты оставляем — вдруг откроют доступ.
MODELS = [
    {
        "id": "auto",
        "name": "Auto",
        "hex": "cf41b0e0dd7d53e5",
        "mode": 1,
        "desc": "автовыбор Google (анонимно — 3.5 Flash-Lite)",
    },
    {
        "id": "flash",
        "name": "Gemini 3.6 Flash",
        "hex": "fbb127bbb056c959",
        "mode": 1,
        "desc": "основной маршрут Flash (анонимно может ответить Lite)",
    },
    {
        "id": "lite",
        "name": "Gemini 3.5 Flash-Lite",
        "hex": "cf41b0e0dd7d53e5",
        "mode": 1,
        "desc": "самый стабильный анонимный вариант",
    },
]

ALIASES = {
    "auto": "auto",
    "авто": "auto",
    "flash": "flash",
    "3.6": "flash",
    "3.6-flash": "flash",
    "gemini-3.6-flash": "flash",
    "lite": "lite",
    "3.5": "lite",
    "3.5-lite": "lite",
    "gemini-3.5-flash-lite": "lite",
    "flash-lite": "lite",
}

_BL_CACHE = {"value": "", "ts": 0.0}
_BL_TTL = 3600.0
_MODEL_RE = re.compile(r'\\"([0-9a-f]{16})\\",null,null,\\"([^"\\]{1,40})\\"')
_CODE_RE = re.compile(
    r"```(?:python|javascript|text)\?code_(?:reference|stdout)"
    r"&code_event_index=\d+\n.*?```\n?",
    re.S,
)
# Internal Google UI tags that may leak into text responses.
_UI_TAG_RE = re.compile(r"<FollowUp\b[^>]*>.*?</FollowUp\s*>", re.S | re.I)
_UI_TAG_SELF_RE = re.compile(r"<FollowUp\b[^>]*/\s*>", re.I)
_EXTRA_BLANKS_RE = re.compile(r"\n{3,}")
_FAILURE_PHRASES = (
    "i encountered an error doing what you asked",
    "something went wrong. please try again",
    "an error occurred. please try again",
    "i'm having trouble responding right now",
)


def is_failure_reply(text: str) -> bool:
    low = " ".join(text.lower().split())
    return len(low) < 400 and any(p in low for p in _FAILURE_PHRASES)


class GeminiError(RuntimeError):
    """Понятная пользователю ошибка запроса."""


@dataclass
class Reply:
    text: str
    model: str
    elapsed: float

    @property
    def chars(self) -> int:
        return len(self.text)


def resolve_model(name: str | None) -> dict:
    key = (name or "auto").strip().lower()
    model_id = ALIASES.get(key)
    if model_id is None:
        for model in MODELS:
            if key in (model["id"], model["name"].lower()):
                return model
        raise GeminiError(f"неизвестная модель: {name}")
    return next(m for m in MODELS if m["id"] == model_id)


def clean_text(text: str) -> str:
    text = re.sub(r"<ElicitationsGroup.*", "", text, flags=re.S)
    text = _UI_TAG_RE.sub("", text)
    text = _UI_TAG_SELF_RE.sub("", text)
    text = _CODE_RE.sub("", text)
    # clean excessive blank lines left after tag/block removal
    text = _EXTRA_BLANKS_RE.sub("\n\n", text)
    return text.strip()


def parse_frame_texts(line: str) -> list[str]:
    """Достаёт тексты ответа из одной строки потока wrb.fr."""
    if '"wrb.fr"' not in line or len(line) < 100:
        return []
    try:
        arr = json.loads(line)
    except ValueError:
        return []
    if not isinstance(arr, list) or not arr:
        return []
    first = arr[0]
    if not isinstance(first, list) or len(first) < 3:
        return []
    payload = first[2]
    if not isinstance(payload, str) or len(payload) < 40:
        return []
    try:
        inner = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(inner, list) or len(inner) <= 4:
        return []
    parts = inner[4]
    if not isinstance(parts, list):
        return []
    out: list[str] = []
    for part in parts:
        if isinstance(part, list) and len(part) > 1 and isinstance(part[1], list):
            out.extend(t for t in part[1] if isinstance(t, str) and t)
    return out


def parse_bl(page: str) -> str | None:
    """Достаёт build-id `cfb2h` из HTML страницы Gemini."""
    match = re.search(r'"cfb2h":"([^"]{10,120})"', page or "")
    return match.group(1) if match else None


def fetch_bl(force: bool = False, timeout: float = 25) -> str:
    if _BL_CACHE["value"] and not force and time.time() - _BL_CACHE["ts"] < _BL_TTL:
        return _BL_CACHE["value"]
    req = urllib.request.Request(
        PAGE_URL,
        headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        page = resp.read().decode("utf-8", "replace")
    bl = parse_bl(page)
    if not bl:
        raise GeminiError("Gemini: на странице нет cfb2h (изменился протокол?)")
    _BL_CACHE.update(value=bl, ts=time.time())
    return _BL_CACHE["value"]


class Gemini:
    def __init__(self, model: str | dict = "auto", timeout: float = 60):
        self.model = model if isinstance(model, dict) else resolve_model(model)
        self.timeout = timeout
        self.last_text = ""
        self.last_model_name = self.model["name"]
        self.last_elapsed = 0.0

    # ── публичный API ──────────────────────────────────────────────────
    def ask(self, prompt: str, on_delta=None) -> Reply:
        """Задаёт вопрос. on_delta(текст) вызывается по мере поступления."""
        attempts = [self.model]
        for model in MODELS:
            if model["id"] != self.model["id"] and len(attempts) < 2:
                attempts.append(model)

        errors: list[str] = []
        for index, model in enumerate(attempts):
            emitted = {"value": False}

            def delta_cb(chunk: str) -> None:
                emitted["value"] = True
                if on_delta is not None:
                    on_delta(chunk)

            try:
                if index:
                    time.sleep(0.8)
                return self._ask_once(prompt, model, delta_cb if on_delta else None)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — нужен фолбэк
                if emitted["value"]:
                    raise GeminiError(f"ответ оборвался: {exc}") from exc
                errors.append(f"{model['name']}: {exc}")
                _BL_CACHE["value"] = ""

        raise GeminiError("; ".join(errors) or "Gemini недоступен")

    # ── внутрянка ──────────────────────────────────────────────────────
    def _ask_once(self, prompt: str, model: dict, on_delta) -> Reply:
        started = time.time()
        self.last_text = ""
        bl = fetch_bl(timeout=min(25, self.timeout))
        request_id = str(uuid.uuid4())

        inner: list = [None] * 99
        inner[0] = [prompt, 0, None, None, None, None, 0]
        inner[1] = ["en"]
        inner[2] = ["", "", "", None, None, None, None, None, None, ""]
        inner[6] = [0]
        inner[7] = 1
        inner[10] = 1
        inner[11] = 0
        inner[17] = [[0]]
        inner[18] = 0
        inner[27] = 1
        inner[30] = [4]
        inner[41] = [1]
        inner[53] = 0
        inner[59] = request_id
        inner[61] = []
        inner[68] = 1
        inner[79] = model["mode"]
        inner[80] = 1
        inner[91] = 0
        inner[96] = 0

        outer = [None, json.dumps(inner, separators=(",", ":"))]
        body = urllib.parse.urlencode(
            {"f.req": json.dumps(outer, separators=(",", ":"))}
        ).encode()

        url = (
            f"{RPC_URL}?bl={urllib.parse.quote(bl)}"
            f"&hl=en&_reqid={int(time.time() * 1000) % 1000000}&rt=c"
        )
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": "https://gemini.google.com",
                "Referer": PAGE_URL,
                "X-Same-Domain": "1",
                "X-Goog-AuthUser": "0",
                "x-goog-ext-73010989-jspb": "[0]",
                "x-goog-ext-73010990-jspb": "[0,0,0]",
                "x-goog-ext-525001261-jspb": json.dumps(
                    [1, None, None, None, model["hex"]], separators=(",", ":")
                ),
                "x-goog-ext-525005358-jspb": json.dumps(
                    [request_id, 1], separators=(",", ":")
                ),
            },
        )

        try:
            response = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            if exc.code == 429:
                raise GeminiError(
                    "Google временно ограничил запросы (429). Подожди минуту."
                ) from exc
            if exc.code in (401, 403):
                raise GeminiError(
                    "Google отклонил анонимный запрос (403). Попробуй позже."
                ) from exc
            raise GeminiError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise GeminiError(f"нет сети: {exc.reason}") from exc

        with response:
            try:
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace")
                    texts = parse_frame_texts(line)
                    if texts:
                        candidate = clean_text(texts[-1])
                        if candidate and is_failure_reply(candidate):
                            raise GeminiError("Gemini вернул служебную ошибку")
                        if candidate and candidate != self.last_text:
                            old = self.last_text
                            if candidate.startswith(old):
                                chunk = candidate[len(old):]
                            else:
                                chunk = candidate
                            self.last_text = candidate
                            if chunk and on_delta is not None:
                                on_delta(chunk)
                    match = None
                    for match in _MODEL_RE.finditer(line):
                        pass
                    if match and match.group(2):
                        self.last_model_name = match.group(2)
            except http.client.IncompleteRead as exc:
                raise GeminiError("соединение оборвалось") from exc

        text = clean_text(self.last_text)
        self.last_text = text
        self.last_elapsed = time.time() - started
        if not text:
            raise GeminiError("Gemini вернул пустой ответ")
        if is_failure_reply(text):
            raise GeminiError("Gemini вернул служебную ошибку")
        return Reply(text=text, model=self.last_model_name, elapsed=self.last_elapsed)


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    body = re.sub(r"<[^>]+>", " ", body)
    return " ".join(body.split())[:180]
