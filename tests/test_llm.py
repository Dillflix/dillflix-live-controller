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


async def test_strict_wire_schema_omits_length_ceilings_but_local_model_retains_them():
    import copy
    import json

    from pydantic import ValidationError

    from controller.prime_player.matching import Choice

    original = Choice.model_json_schema()
    saved = copy.deepcopy(original)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{
            'finish_reason': 'stop', 'message': {'content': json.dumps({
                'content_id': None, 'viewing_option_id': None,
                'reason': 'No candidates', 'evidence': [],
            })},
        }]})

    client = ChatClient(ExecutorConfig(base_url='https://model.invalid/v1', structured_output='json_schema'),
                        transport=httpx.MockTransport(respond))
    try:
        answer = await client.completion('test', [], original)
    finally:
        await client.close()
    Choice.model_validate_json(answer)
    response_format = requests[0]['response_format']
    assert response_format['type'] == 'json_schema'
    assert response_format['json_schema']['strict'] is True
    wire = response_format['json_schema']['schema']
    assert 'maxLength' not in json.dumps(wire)
    assert wire['additionalProperties'] is False
    assert set(wire['required']) == set(original['properties'])
    assert wire['properties']['reason']['minLength'] == 1
    assert wire['properties']['evidence']['maxItems'] == 10
    assert wire['properties']['content_id']['anyOf'] == [{'type': 'string'}, {'type': 'null'}]
    assert original == saved == Choice.model_json_schema()
    valid = json.loads(answer)
    for field, limit in [('content_id', 256), ('viewing_option_id', 1024), ('reason', 2000)]:
        with pytest.raises(ValidationError):
            Choice.model_validate({**valid, field: 'x' * (limit + 1)})


@pytest.mark.parametrize('status, body', [
    (200, b'{"choices":[{"finish_reason":"stop","message":{"content":"answer secret-value"}}]}'),
    (400, b'{"error":{"message":"bad grammar secret-value","api_key":"hidden"}}'),
    (200, b'not JSON secret-value'),
    (400, b'x' * 17000),
])
async def test_model_trace_captures_prompt_and_response_on_success_and_failure(status, body):
    import copy
    import json

    from controller.model_diagnostics import capture_model_calls

    records = []
    client = ChatClient(ExecutorConfig(base_url='https://model.invalid/v1', api_key='secret-value'),
                        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body)))
    try:
        with capture_model_calls(lambda r: records.append(copy.deepcopy(r))):
            try:
                await client.completion('test', [{'role': 'user', 'content': 'match this event'}])
            except ExecutorError:
                pass
    finally:
        await client.close()
    assert len(records) == 2
    assert records[0]['state'] == 'pending'
    assert records[0]['response'] is None
    assert records[1]['http_status'] == status
    assert records[1]['request']['json']['messages'][0]['content'] == 'match this event'
    assert records[1]['response']['truncated'] is (len(body) > 16384)
    assert 'secret-value' not in json.dumps(records)
    assert 'hidden' not in json.dumps(records)
    assert records[1]['finished_at']


async def test_model_trace_retains_cancelled_request():
    import asyncio
    import copy

    from controller.model_diagnostics import capture_model_calls

    records = []
    entered = asyncio.Event()

    async def respond(request):
        entered.set()
        await asyncio.Event().wait()

    client = ChatClient(ExecutorConfig(base_url='https://model.invalid/v1'),
                        transport=httpx.MockTransport(respond))
    try:
        with capture_model_calls(lambda r: records.append(copy.deepcopy(r))):
            task = asyncio.create_task(client.completion('test', []))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert records[-1]['state'] == 'cancelled'
        assert records[-1]['http_status'] is None
    finally:
        await client.close()
