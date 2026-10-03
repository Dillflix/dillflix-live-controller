import httpx
import pytest

from controller.executor.config import ExecutorConfig
from controller.executor.models import ExecutorError
from controller.llm import ChatClient


@pytest.mark.parametrize('status', [400, 429, 503])
async def test_provider_rejection_retained_without_credentials(status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {
            'type': 'invalid_request_error', 'code': 'unsupported_parameter',
            'param': 'max_tokens',
            'message': 'Use max_completion_tokens. secret-value Bearer private sk-other-secret',
            'request': 'must not retain arbitrary fields',
        }})

    client = ChatClient(ExecutorConfig(base_url='https://model.invalid/v1', api_key='secret-value'),
                        transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ExecutorError) as caught:
            await client.completion('test', [])
        error = caught.value
        assert error.code == 'model_http_error'
        assert error.retryable is (status != 400)
        assert 'param=max_tokens' in error.message
        assert 'Use max_completion_tokens' in error.message
        for secret in ('secret-value', 'private', 'sk-other-secret', 'arbitrary'):
            assert secret not in error.message
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize('body', [b'<html>proxy failure</html>', b'x' * 20000, b'[]'])
async def test_unstructured_and_oversized_error_bodies_not_exposed(body):
    client = ChatClient(ExecutorConfig(base_url='https://model.invalid/v1'),
                        transport=httpx.MockTransport(lambda request: httpx.Response(400, content=body)))
    try:
        with pytest.raises(ExecutorError) as caught:
            await client.completion('test', [])
        assert caught.value.code == 'model_http_error'
        assert len(caught.value.message) < 150
        assert 'proxy failure' not in caught.value.message
    finally:
        await client.close()
