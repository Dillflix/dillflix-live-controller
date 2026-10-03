"""Bounded OpenAI-compatible structured text/image completion transport."""

import asyncio
import json

import httpx

from .executor.models import ExecutorError


def strict_schema(schema):
    """Keep optional fields backward compatible locally, but required/null on the wire."""
    if isinstance(schema, list):
        return [strict_schema(value) for value in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: strict_schema(value) for key, value in schema.items() if key != "default"}
    if result.get("type") == "object" and "properties" in result:
        result["required"] = list(result["properties"])
        result["additionalProperties"] = False
    return result


class ChatClient:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(config.model_timeout, connect=min(10, config.model_timeout), pool=5),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            transport=transport,
            headers={"Authorization": "Bearer " + config.api_key} if config.api_key else {},
        )
        self.lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def completion(self, model, messages, schema=None, name="tv_observation", max_tokens=1800):
        body = {
            **self.config.model_options,
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
        }
        if schema is not None:
            if self.config.structured_output == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": strict_schema(schema)},
                }
            else:
                body["response_format"] = {"type": "json_object"}
                body["messages"] = [
                    *messages,
                    {"role": "user", "content": "Return JSON matching this schema: " + json.dumps(schema)},
                ]
        try:
            # Bound queueing plus network plus decoding, not only inactivity per socket read.
            async with asyncio.timeout(self.config.model_timeout):
                async with self.lock:
                    async with self.client.stream("POST", "chat/completions", json=body) as response:
                        if response.status_code != 200:
                            retryable = response.status_code in {408, 429} or response.status_code >= 500
                            raise ExecutorError(
                                "model_http_error",
                                f"Model service returned HTTP {response.status_code}",
                                retryable=retryable,
                            )
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 512 * 1024:
                                raise ExecutorError("model_output_limit", "Model response exceeded its limit")
                payload = json.loads(raw)
                choices = payload.get("choices")
                if (
                    not isinstance(choices, list)
                    or len(choices) != 1
                    or choices[0].get("finish_reason") != "stop"
                ):
                    raise ExecutorError("model_incomplete", "Model response was incomplete or ambiguous")
                content = choices[0]["message"]["content"]
                if not isinstance(content, str) or len(content) > 32768:
                    raise ExecutorError("model_invalid", "Model response has no bounded text answer")
                return content
        except ExecutorError:
            raise
        except (httpx.HTTPError, TimeoutError) as error:
            raise ExecutorError(
                "model_unavailable", "Model service was unavailable or exceeded its deadline"
            ) from error
        except (ValueError, KeyError, TypeError) as error:
            raise ExecutorError("model_invalid", "Model service returned malformed data") from error
