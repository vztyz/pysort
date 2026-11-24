"""Optional LLM layer: only used for files the rules are unsure about."""
import json
import os
import re
import urllib.error
import urllib.request

ALLOWED = {
    "user_project",
    "user_loose",
    "unknown",
    "venv",
    "third_party",
    "system_python",
    "system_other",
    "cache",
}

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "ollama": "qwen2.5-coder:7b",
}

SYSTEM_PROMPT = """Ты помогаешь разработчику разобрать Python-файлы на его Windows-компьютере.
Для каждого файла определи, чем он является:
- user_project: часть личного проекта пользователя (его код)
- user_loose: его собственный файл/скрипт вне оформленного проекта
- third_party: код сторонней библиотеки, установленной пакетным менеджером (site-packages, conda pkgs)
- venv: файл внутри виртуального окружения / conda env
- system_python: стандартная библиотека установленного Python
- system_other: python, встроенный внутрь другой программы (Program Files, AppData, Windows)
- cache: кэш, сгенерированные/временные файлы
- unknown: непонятно

Подсказки: путь, размер, дата изменения и фрагмент кода. Имена вроде site-packages, Lib, dist-packages,
venv, .venv, conda-meta, __pycache__, pip/cache — почти всегда не пользовательский код.
Личные проекты обычно лежат в Desktop/Documents/Downloads/PycharmProjects/Projects/source и имеют .git/pyproject.
Отвечай СТРОГО валидным JSON без markdown:
{"items":[{"path":"<точный путь>","label":"<один из списка>","confidence":0.0,"reason":"кратко по-русски"}]}
Сохраняй все пути из входных данных и их порядок."""


class AIError(RuntimeError):
    pass


class AIClient:
    def __init__(self, backend, model=None, api_key=None, base_url=None, timeout=120):
        self.backend = backend
        self.timeout = timeout
        self.api_key = api_key or ""
        if backend == "openai":
            self.base = (base_url or "https://api.openai.com/v1").rstrip("/")
            self.model = model or DEFAULT_MODELS["openai"]
        elif backend == "deepseek":
            self.base = (base_url or "https://api.deepseek.com/v1").rstrip("/")
            self.model = model or DEFAULT_MODELS["deepseek"]
        elif backend == "ollama":
            self.base = (base_url or "http://127.0.0.1:11434").rstrip("/")
            self.model = model or DEFAULT_MODELS["ollama"]
        else:
            raise AIError(f"неизвестный AI-бэкенд: {backend}")

    # ---- transport -------------------------------------------------
    def _post(self, url, payload, headers):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise AIError(f"HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            raise AIError(f"{type(exc).__name__}: {exc}") from exc

    def chat(self, user_text):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        if self.backend == "ollama":
            data = self._post(
                self.base + "/api/chat",
                {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0},
                },
                {"Content-Type": "application/json"},
            )
            return (data.get("message") or {}).get("content", "")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        data = self._post(self.base + "/chat/completions", payload, headers)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise AIError(f"неожиданный ответ: {str(data)[:300]}") from exc


def _excerpt(path, limit=1800):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(limit)
    except OSError:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.replace("\x00", "")


def parse_reply(text, expected_paths):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise AIError("ответ не содержит JSON")
        data = json.loads(m.group(0))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise AIError("в JSON нет массива items")

    expected = {p.lower(): p for p in expected_paths}
    out = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path", ""))
        real = expected.get(raw.lower())
        if not real:
            continue
        label = str(item.get("label", "")).strip().lower()
        if label not in ALLOWED:
            continue
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out[real] = {
            "label": label,
            "confidence": max(0.0, min(1.0, conf)),
            "reason": str(item.get("reason", ""))[:300],
        }
    return out


def classify_paths(client, entries, with_content=True, batch_size=25, log=print):
    """entries: list of dicts {path,size,mtime,markers}. Yields {path: verdict}."""
    for start in range(0, len(entries), batch_size):
        chunk = entries[start : start + batch_size]
        payload = []
        for e in chunk:
            item = {
                "path": e["path"],
                "size_kb": round(e.get("size", 0) / 1024, 1),
                "modified": e.get("mtime_str", ""),
                "hints": e.get("markers", []),
            }
            if with_content:
                snippet = _excerpt(e["path"])
                if snippet:
                    item["excerpt"] = snippet
            payload.append(item)
        user_text = (
            "Классифицируй эти файлы. Верни JSON ровно с этими путями.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        for attempt in (1, 2):
            try:
                reply = client.chat(user_text)
                verdicts = parse_reply(reply, [e["path"] for e in chunk])
                break
            except (AIError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    log(f"    ! батч пропущен: {exc}")
                    verdicts = {}
                else:
                    log(f"    … повтор после ошибки: {exc}")
        for path, verdict in verdicts.items():
            yield path, verdict
