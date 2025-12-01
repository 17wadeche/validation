from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    refresh_path: str = "/tokens/refresh"
    temperature: float | None = 0.0
    max_tokens: int | None = 32768
    last_refresh: bool = field(default=False, init=False)

    DEFAULT_BASE_URL = "https://api.gpt.medtronic.com"
    DEFAULT_API_VERSION = "3.0"
    DEFAULT_PATH_TEMPLATE = "/models/{model}"
    DEFAULT_TEMPERATURE = 0.0
    DEFAULT_MAX_TOKENS = 32768

    def generate_completion(
        self,
        prompt: str | None = None,
        *,
        model: str = "gpt-41",
        messages: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        # Reset refresh flag for this request
        self.last_refresh = False
        if messages is None:
            if not prompt or not prompt.strip():
                raise MedtronicGPTError("Prompt is empty; supply a template, examples, and code context.")
            payload_messages = [{"role": "user", "content": prompt}]
        else:
            if not messages:
                raise MedtronicGPTError("Message history is empty; provide at least one message.")
            payload_messages = messages

        def _send_once(current_api_token: str) -> str:
            path = self.path_template.format(model=parse.quote(model, safe=""))
            if not path.startswith("/"):
                path = f"/{path}"
            url = f"{self.base_url.rstrip('/')}{path}"
            payload_body: dict = {"messages": payload_messages}
            payload_temperature = temperature if temperature is not None else self.temperature
            if payload_temperature is not None:
                payload_body["temperature"] = float(payload_temperature)

            payload_max_tokens = max_tokens if max_tokens is not None else self.max_tokens
            if payload_max_tokens is not None:
                payload_body["max_tokens"] = int(payload_max_tokens)

            payload = json.dumps(payload_body).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "subscription-key": self.subscription_key,
                "api-token": current_api_token,
                "refresh-token": self.refresh_token,
                "api-version": self.api_version,
            }

            req = request.Request(url, data=payload, headers=headers)
            with request.urlopen(req) as resp:  # nosec: B310
                body = resp.read().decode("utf-8")
            return self._extract_content(body)

        try:
            return _send_once(self.api_token)
        except error.HTTPError as exc:  # pragma: no cover - network
            if exc.code == 401 and self.refresh_token:
                try:
                    refresh_data = self.refresh_tokens()
                except MedtronicGPTError:
                    pass
                else:
                    new_token = refresh_data.get("apiToken") or refresh_data.get("api_token")
                    if new_token:
                        self.api_token = new_token
                    new_refresh = refresh_data.get("refreshToken") or refresh_data.get("refresh_token")
                    if new_refresh:
                        self.refresh_token = new_refresh
                    try:
                        return _send_once(self.api_token)
                    except error.HTTPError as retry_exc:  # pragma: no cover - network
                        raise MedtronicGPTError(
                            f"MedtronicGPT request failed after refresh ({retry_exc.code}): {retry_exc.reason} (URL: {retry_exc.geturl()})"
                        ) from retry_exc
                    except error.URLError as retry_exc:  # pragma: no cover - network
                        raise MedtronicGPTError(
                            f"MedtronicGPT connection error after refresh: {retry_exc.reason}"
                        ) from retry_exc

            raise MedtronicGPTError(
                f"MedtronicGPT request failed ({exc.code}): {exc.reason} (URL: {exc.geturl()})"
            ) from exc
        except error.URLError as exc:  # pragma: no cover - network
            raise MedtronicGPTError(f"MedtronicGPT connection error: {exc.reason}") from exc

    def refresh_tokens(self) -> dict:
        """Refresh the API token using the stored refresh token.

        Returns the parsed JSON response on success and updates in-place tokens.
        """

        if not self.refresh_token:
            raise MedtronicGPTError("No refresh token provided; cannot refresh API token.")

        path = self.refresh_path
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.base_url.rstrip('/')}{path}"

        headers = {
            "subscription-key": self.subscription_key,
            "api-token": self.api_token,
            "refresh-token": self.refresh_token,
            "api-version": self.api_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        req = request.Request(url, data=json.dumps({}).encode("utf-8"), headers=headers)
        try:
            with request.urlopen(req) as resp:  # nosec: B310
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:  # pragma: no cover - network
            raise MedtronicGPTError(
                f"Token refresh failed ({exc.code}): {exc.reason} (URL: {url})"
            ) from exc
        except error.URLError as exc:  # pragma: no cover - network
            raise MedtronicGPTError(f"MedtronicGPT connection error during refresh: {exc.reason}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:  # pragma: no cover - parsing
            raise MedtronicGPTError("Token refresh returned an invalid response.") from exc

        new_api_token = data.get("apiToken") or data.get("api_token")
        new_refresh_token = data.get("refreshToken") or data.get("refresh_token")

        if new_api_token:
            self.api_token = new_api_token
        if new_refresh_token:
            self.refresh_token = new_refresh_token

        self.last_refresh = True
        return data

    @staticmethod
    def _extract_content(body: str) -> str:
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
