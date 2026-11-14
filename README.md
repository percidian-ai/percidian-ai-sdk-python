# percidian

The Python client for Percept One, Percidian's extraction model: send a document's text, get back a typed knowledge graph,
every node, property and edge cited to the text it comes from.

```sh
pip install percidian
```

Python 3.10 or later.

## Quickstart

Make an API key in the console (console.percidian.ai) and keep it in your server's environment as `PERCIDIAN_API_KEY`;
the client reads it from there (or pass `api_key=`).

```python
from percidian import Percidian

client = Percidian()
result = client.extract(model="percept-1", text="Maya Chen is the CEO of Northstar Labs, based in Boston.")
for node in result.graph.nodes:
    print(node.type, node.label)
```

`AsyncPercidian` has the same methods, awaited.

## Waiting for results

`extract` and `fill` return the job's result however long the job runs. The API holds the request up to 280 seconds and
answers with the job if it is still running; the client then polls `jobs.get_result` until the job ends. A failed job
raises `ConflictError` with the code `job_failed`. Their requests time out after 330 seconds at the least.

- `prefer="respond-async"` returns the job (`JobAccepted`) at once: fetch its result later with
  `client.jobs.get_result(job.id)`, or take its webhook.
- `format="neo4j"` returns a `Neo4JGraph`, `format="jsonld"` the JSON-LD as a dict, and `format="csv"` (with `table`) the
  CSV text.

## Batches

A batch is JSON Lines, one request a line, sent as bytes:

```python
import json

lines = [
    {"custom_id": "memo-1", "method": "POST", "url": "/v1/extract", "body": {"model": "percept-1", "text": "..."}},
    {"custom_id": "memo-2", "method": "POST", "url": "/v1/extract", "body": {"model": "percept-1", "text": "..."}},
]
batch = client.batches.create(request="\n".join(json.dumps(line) for line in lines).encode())
# Later: one BatchResult a line, matched to your requests by custom_id.
for line in client.batches.get_results(batch.id).splitlines():
    print(json.loads(line)["custom_id"])
```

## Errors and retries

Each error status has its class (`BadRequestError`, `UnauthorizedError`, `PaymentRequiredError`, `TooManyRequestsError`,
`ConflictError`, ...), and its `body.error.code` says what went wrong:

```python
from percidian import TooManyRequestsError

try:
    client.extract(model="percept-1", text=text)
except TooManyRequestsError as error:
    print(error.body.error.code, error.body.error.limit, error.headers.get("retry-after"))
```

The client retries 408, 409, 429 and 5xx answers twice, waiting as `Retry-After` says (60 seconds at most); set
`max_retries` on the client or in `request_options`. Every `extract`, `fill`, `match` and `batches.create` carries an
`Idempotency-Key` (a new UUID unless you pass `idempotency_key`), the same on each retry, so a retry never runs or bills
twice. A dropped connection during `extract` or `fill` is retried the same way.

## Webhooks

Percept signs each webhook as [Standard Webhooks](https://www.standardwebhooks.com) does. Check one with the raw body,
exactly as received:

```python
from percidian import WebhookVerificationError, verify_webhook

try:
    event = verify_webhook(secret, request.headers, request.get_data())
except WebhookVerificationError:
    ...  # answer 400
```

`secret` is the endpoint's `whsec_` secret (`client.webhooks.endpoints.create` or `get_secret`), or for a request's
`webhook_url` the organization's (`client.webhooks.get_secret()`). A timestamp more than 5 minutes off is refused.

## Reference

Every method is in [reference.md](./reference.md).
