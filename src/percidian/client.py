"""Percidian and AsyncPercidian: the API client, with extract and fill that wait for the job's result."""

import asyncio
import functools
import time
import typing
import uuid

from .base_client import AsyncBasePercidian, BasePercidian
from .core.api_error import ApiError
from .core.parse_error import ParsingError
from .core.pydantic_utilities import parse_obj_as
from .errors.conflict_error import ConflictError
from .types.job_accepted import JobAccepted

F = typing.TypeVar("F", bound=typing.Callable[..., typing.Any])

# Above the API's 280-second wait.
WAIT_TIMEOUT_SECONDS = 330.0
# GET /v1/jobs/{job_id}/result answers 409 result_not_ready, with Retry-After: 5, while the job runs.
POLL_SECONDS = 5.0


def _like(generated: F) -> typing.Callable[[typing.Callable[..., typing.Any]], F]:
    """Gives a method the generated one's parameters, types and docs."""
    return lambda method: typing.cast(F, functools.wraps(generated)(method))


def _request(kwargs: typing.Dict[str, typing.Any], client_timeout: typing.Optional[float]) -> typing.Dict[str, typing.Any]:
    """The call's arguments with its own Idempotency-Key, a timeout above the wait and retries of dropped connections."""
    options = dict(kwargs.get("request_options") or {})
    options.setdefault("timeout", max(client_timeout or 0, WAIT_TIMEOUT_SECONDS))
    options.setdefault("retry_remote_protocol_errors", True)
    return {**kwargs, "idempotency_key": kwargs.get("idempotency_key") or str(uuid.uuid4()), "request_options": options}


def _accepted(error: Exception, kwargs: typing.Dict[str, typing.Any]) -> typing.Tuple[typing.Optional[str], typing.Any]:
    """A 202 is (the job's id, to poll it), or (None, the job) with prefer="respond-async"; else (None, the formatted result)."""
    if isinstance(error, ParsingError) and error.status_code == 202:
        if "respond-async" in (kwargs.get("prefer") or ""):
            return None, parse_obj_as(JobAccepted, error.body)
        return error.body["id"], None
    return None, _formatted(error, kwargs.get("format"))


def _formatted(error: Exception, result_format: typing.Any) -> typing.Any:
    """A `jsonld` or `csv` result, which is not a `Result`."""
    if isinstance(error, ParsingError) and error.status_code == 200 and result_format == "jsonld":
        return error.body
    if type(error) is ApiError and error.status_code == 200 and result_format == "csv":
        return error.body
    raise error


def _raw_json_lines(http: typing.Any) -> None:
    """Sends a JSON Lines body (POST /v1/batches) as the caller's bytes or text."""
    request = http.request

    def send(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        if kwargs.get("json") is not None and (kwargs.get("headers") or {}).get("content-type") == "application/jsonl":
            kwargs["content"] = kwargs.pop("json")
        return request(*args, **kwargs)

    http.request = send


def _poll_after(error: ConflictError) -> typing.Optional[float]:
    """Seconds before asking again while the job runs (409 result_not_ready); None for any other 409 (job_failed)."""
    if error.body.error.code != "result_not_ready":
        return None
    try:
        return float((error.headers or {}).get("retry-after", POLL_SECONDS))
    except ValueError:
        return POLL_SECONDS


class Percidian(BasePercidian):
    @_like(BasePercidian.__init__)
    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        _raw_json_lines(self._client_wrapper.httpx_client)

    @_like(BasePercidian.extract)
    def extract(self, **kwargs: typing.Any) -> typing.Any:
        return self._waited(BasePercidian.extract, kwargs)

    @_like(BasePercidian.fill)
    def fill(self, **kwargs: typing.Any) -> typing.Any:
        return self._waited(BasePercidian.fill, kwargs)

    def _waited(self, method: typing.Callable[..., typing.Any], kwargs: typing.Dict[str, typing.Any]) -> typing.Any:
        kwargs = _request(kwargs, self._client_wrapper.get_timeout())
        try:
            return method(self, **kwargs)
        except (ApiError, ParsingError) as error:
            job_id, value = _accepted(error, kwargs)
        if job_id is None:
            return value
        while True:
            try:
                return self.jobs.get_result(job_id)
            except ConflictError as error:
                delay = _poll_after(error)
                if delay is None:
                    raise
                time.sleep(delay)
            except (ApiError, ParsingError) as error:
                return _formatted(error, kwargs.get("format"))


class AsyncPercidian(AsyncBasePercidian):
    @_like(AsyncBasePercidian.__init__)
    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        _raw_json_lines(self._client_wrapper.httpx_client)

    @_like(AsyncBasePercidian.extract)
    async def extract(self, **kwargs: typing.Any) -> typing.Any:
        return await self._waited(AsyncBasePercidian.extract, kwargs)

    @_like(AsyncBasePercidian.fill)
    async def fill(self, **kwargs: typing.Any) -> typing.Any:
        return await self._waited(AsyncBasePercidian.fill, kwargs)

    async def _waited(self, method: typing.Callable[..., typing.Any], kwargs: typing.Dict[str, typing.Any]) -> typing.Any:
        kwargs = _request(kwargs, self._client_wrapper.get_timeout())
        try:
            return await method(self, **kwargs)
        except (ApiError, ParsingError) as error:
            job_id, value = _accepted(error, kwargs)
        if job_id is None:
            return value
        while True:
            try:
                return await self.jobs.get_result(job_id)
            except ConflictError as error:
                delay = _poll_after(error)
                if delay is None:
                    raise
                await asyncio.sleep(delay)
            except (ApiError, ParsingError) as error:
                return _formatted(error, kwargs.get("format"))
