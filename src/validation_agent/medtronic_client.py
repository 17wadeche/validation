from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, parse, request


class MedtronicGPTError(Exception):
    """Raised when the MedtronicGPT service cannot return a completion."""


@dataclass
class MedtronicGPTClient:
    subscription_key: str
    api_token: str
    refresh_token: str
    base_url: str = "https://api.gpt.medtronic.com"
    api_version: str = "3.0"
    path_template: str = "/models/{model}"

    DEFAULT_BASE_URL = "https://api.gpt.medtronic.com"
    DEFAULT_API_VERSION = "3.0"
    DEFAULT_PATH_TEMPLATE = "/models/{model}"

    def generate_completion(
        self,
        prompt: str | None = None,
        *,
        model: str = "gpt-41",
        messages: list[dict] | None = None,
    ) -> str:
        if messages is None:
            if not prompt or not prompt.strip():
                raise MedtronicGPTError("Prompt is empty; supply a template, examples, and code context.")
            payload_messages = [{"role": "user", "content": prompt}]
        else:
            if not messages:
                raise MedtronicGPTError("Message history is empty; provide at least one message.")
            payload_messages = messages

        path = self.path_template.format(model=parse.quote(model, safe=""))
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.base_url.rstrip('/')}{path}"
        payload = json.dumps({"messages": payload_messages}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "subscription-key": self.subscription_key,
            "api-token": self.api_token,
            "refresh-token": self.refresh_token,
            "api-version": self.api_version,
        }

        req = request.Request(url, data=payload, headers=headers)
        try:
            with request.urlopen(req) as resp:  # nosec: B310
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:  # pragma: no cover - network
            raise MedtronicGPTError(
                f"MedtronicGPT request failed ({exc.code}): {exc.reason} (URL: {url})"
            ) from exc
        except error.URLError as exc:  # pragma: no cover - network
            raise MedtronicGPTError(f"MedtronicGPT connection error: {exc.reason} (URL: {url})") from exc

        try:
            data = json.loads(body)
            if "choices" in data and data["choices"]:
                message = data["choices"][0].get("message", {}).get("content")
                if message:
                    return message
            if "content" in data:
                return str(data["content"])
        except json.JSONDecodeError:
            pass

        raise MedtronicGPTError("Unexpected MedtronicGPT response format.")
