# extract waiting through a 202, retries keeping their Idempotency-Key, typed errors, and webhook verification against a
# delivery as Percept signs one. No network: httpx's MockTransport answers.
import base64
import json
import pathlib
import typing

import httpx
import pytest

from percidian import (
    AsyncPercidian,
    ConflictError,
    JobAccepted,
    Percidian,
    TooManyRequestsError,
    WebhookVerificationError,
    verify_webhook,
)

SDKS = next(p for p in pathlib.Path(__file__).resolve().parents if (p / "responses.json").is_file())
RESPONSES = json.loads((SDKS / "responses.json").read_text())
WEBHOOK = json.loads((SDKS / "webhook-fixture.json").read_text())
JOB_ID = RESPONSES["job_accepted"]["id"]
TEXT = "Ada Lovelace wrote the first published program in 1843."

Handler = typing.Callable[[httpx.Request], httpx.Response]


def answers(*responses: httpx.Response) -> typing.Tuple[Handler, typing.List[httpx.Request]]:
    """A transport handler giving these responses in turn, and the requests it was sent."""
    sent: typing.List[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return queue.pop(0)

    return handler, sent


def accepted() -> httpx.Response:
    return httpx.Response(
        202, json=RESPONSES["job_accepted"], headers={"Location": f"/v1/jobs/{JOB_ID}", "Retry-After": "5"}
    )


def test_extract_waits_through_a_202_for_the_result() -> None:
    handler, sent = answers(
        accepted(),
        httpx.Response(409, json=RESPONSES["result_not_ready"], headers={"Retry-After": "0"}),
        httpx.Response(200, json=RESPONSES["result"]),
    )
    client = Percidian(api_key="sk_test", max_retries=0, httpx_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.extract(model="percept-1", text=TEXT)
    assert result.id == RESPONSES["result"]["id"]
    assert [(r.method, r.url.path) for r in sent] == [
        ("POST", "/v1/extract"),
        ("GET", f"/v1/jobs/{JOB_ID}/result"),
        ("GET", f"/v1/jobs/{JOB_ID}/result"),
    ]
    assert sent[0].headers["authorization"] == "Bearer sk_test"
    assert json.loads(sent[0].content) == {"model": "percept-1", "text": TEXT}


def test_a_retried_extract_keeps_its_idempotency_key() -> None:
    handler, sent = answers(
        httpx.Response(429, json=RESPONSES["rate_limited"], headers={"Retry-After": "0"}),
        httpx.Response(200, json=RESPONSES["result"]),
    )
    client = Percidian(api_key="sk_test", httpx_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.extract(model="percept-1", text=TEXT).id == RESPONSES["result"]["id"]
    keys = [r.headers["idempotency-key"] for r in sent]
    assert len(keys) == 2 and keys[0] == keys[1]


def test_errors_are_typed_with_the_api_error_code() -> None:
    handler, _ = answers(httpx.Response(429, json=RESPONSES["rate_limited"], headers={"Retry-After": "30"}))
    client = Percidian(api_key="sk_test", max_retries=0, httpx_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(TooManyRequestsError) as raised:
        client.extract(model="percept-1", text=TEXT)
    assert raised.value.body.error.code == "rate_limited"
    assert raised.value.body.error.limit == "in_flight"


def test_a_failed_job_raises_while_polling() -> None:
    handler, _ = answers(accepted(), httpx.Response(409, json=RESPONSES["job_failed"]))
    client = Percidian(api_key="sk_test", max_retries=0, httpx_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ConflictError) as raised:
        client.extract(model="percept-1", text=TEXT)
    assert raised.value.body.error.code == "job_failed"


def test_respond_async_returns_the_job() -> None:
    handler, sent = answers(accepted())
    client = Percidian(api_key="sk_test", httpx_client=httpx.Client(transport=httpx.MockTransport(handler)))
    job = client.extract(model="percept-1", text=TEXT, prefer="respond-async")
    assert isinstance(job, JobAccepted) and job.id == JOB_ID
    assert len(sent) == 1 and sent[0].headers["prefer"] == "respond-async"


@pytest.mark.parametrize("given", [RESPONSES["batch_requests"].encode(), RESPONSES["batch_requests"]])
def test_batches_create_sends_the_json_lines_as_they_are(given: typing.Any) -> None:
    # As edge.ts createBatch reads them: application/jsonl, one JSON object a line (not one JSON string: 400 batch_invalid),
    # on a retry too.
    handler, sent = answers(
        httpx.Response(429, json=RESPONSES["rate_limited"], headers={"Retry-After": "0"}),
        httpx.Response(202, json=RESPONSES["batch_accepted"]),
    )
    client = Percidian(api_key="sk_test", httpx_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.batches.create(request=given).id == RESPONSES["batch_accepted"]["id"]
    assert [(r.headers["content-type"], r.content) for r in sent] == [("application/jsonl", RESPONSES["batch_requests"].encode())] * 2


async def test_async_batches_create_sends_the_json_lines_as_they_are() -> None:
    handler, sent = answers(httpx.Response(202, json=RESPONSES["batch_accepted"]))
    client = AsyncPercidian(api_key="sk_test", httpx_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.batches.create(request=RESPONSES["batch_requests"].encode())
    assert sent[0].content == RESPONSES["batch_requests"].encode()


async def test_async_extract_waits_through_a_202() -> None:
    handler, sent = answers(accepted(), httpx.Response(200, json=RESPONSES["result"]))
    client = AsyncPercidian(api_key="sk_test", httpx_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await client.extract(model="percept-1", text=TEXT)).id == RESPONSES["result"]["id"]
    assert len(sent) == 2


@pytest.mark.parametrize("secret", [WEBHOOK["secret"], WEBHOOK["previous_secret"]])
def test_verify_webhook_accepts_a_delivery_signed_by_either_secret(secret: str) -> None:
    sent_at = int(WEBHOOK["headers"]["webhook-timestamp"])
    assert verify_webhook(secret, WEBHOOK["headers"], WEBHOOK["body"].encode(), now=sent_at) == json.loads(WEBHOOK["body"])


def test_verify_webhook_refuses_a_changed_body_an_old_timestamp_or_another_secret() -> None:
    headers, body = WEBHOOK["headers"], WEBHOOK["body"]
    sent_at = int(headers["webhook-timestamp"])
    other = "whsec_" + base64.b64encode(b"\x01" * 32).decode()
    for secret, sent, now in [
        (WEBHOOK["secret"], body.replace("succeeded", "failed"), sent_at),
        (WEBHOOK["secret"], body, sent_at + 301),
        (other, body, sent_at),
    ]:
        with pytest.raises(WebhookVerificationError):
            verify_webhook(secret, headers, sent, now=now)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(WEBHOOK["secret"], {k: v for k, v in headers.items() if k != "webhook-signature"}, body, now=sent_at)
