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
    base_url: str = "https://api.gpt-dev.medtronic.com"
    api_version: str = "3.0"

    DEFAULT_BASE_URL = "https://api.gpt-dev.medtronic.com"
    DEFAULT_API_VERSION = "3.0"

    def generate_completion(self, prompt: str, model: str = "gpt-41") -> str:
        if not prompt.strip():
            raise MedtronicGPTError("Prompt is empty; supply template, requirements, examples, and code context.")

        url = (
            f"{self.base_url.rstrip('/')}/models/{parse.quote(model)}"
            f"?{parse.urlencode({'api-version': self.api_version})}"
        )
        payload = json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "api-token": self.api_token,
            "refresh-token": self.refresh_token,
        }

        req = request.Request(url, data=payload, headers=headers)
        try:
            with request.urlopen(req) as resp:  # nosec: B310
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:  # pragma: no cover - network
            raise MedtronicGPTError(f"MedtronicGPT request failed ({exc.code}): {exc.reason}") from exc
        except error.URLError as exc:  # pragma: no cover - network
            raise MedtronicGPTError(f"MedtronicGPT connection error: {exc.reason}") from exc

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
