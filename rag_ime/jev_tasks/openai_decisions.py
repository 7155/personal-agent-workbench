"""Optional, one-shot OpenAI Decisions choice transport; no Agent or Tool loop."""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Mapping

from .types import DecisionUnavailable, GraphError, canonical, probability, text


MODEL = "gpt-6-luna"
ENDPOINT = "https://api.openai.com/v1/decisions"
MAX_RESPONSE_BYTES = 262_144


def choice_request(state: str, questions: Mapping[str, object], *, max_request_bytes: int) -> dict[str, object]:
    """Translate Jev's checked opaque choices into the documented choice schema."""
    output = []
    for name, raw in questions.items():
        if not isinstance(raw, Mapping) or raw.get("type") != "choice":
            raise DecisionUnavailable("OpenAI Decisions adapter requires choice questions")
        criteria = raw.get("criteria")
        if not isinstance(criteria, Mapping) or not 2 <= len(criteria) <= 255:
            raise DecisionUnavailable("OpenAI Decisions choice frontier is invalid")
        try:
            question = {"type": "choice", "name": text(name, "question name", 200),
                "instructions": text(raw.get("instructions"), "instructions", 16000),
                "choices": [{"value": text(value, "choice id", 200), "description": text(description, "criterion", 16000)}
                    for value, description in criteria.items()]}
        except GraphError as exc:
            raise DecisionUnavailable("OpenAI Decisions question is invalid") from exc
        output.append(question)
    if not output:
        raise DecisionUnavailable("OpenAI Decisions request has no questions")
    packet = {"model": MODEL, "input": state, "questions": output}
    if len(canonical(packet).encode("utf-8")) > max_request_bytes:
        raise DecisionUnavailable("OpenAI Decisions request exceeds byte budget; no evidence was truncated")
    return packet


def choice_response(response: object, questions: Mapping[str, object]) -> dict[str, object]:
    """Refusal, missing/duplicate/unknown options and malformed scores stay unavailable."""
    if not isinstance(response, Mapping) or response.get("model") != MODEL:
        raise DecisionUnavailable("OpenAI Decisions returned an unsupported model")
    raw_answers = response.get("answers")
    if not isinstance(raw_answers, list) or len(raw_answers) != len(questions):
        raise DecisionUnavailable("OpenAI Decisions answer count differs from questions")
    answers = {}
    for raw in raw_answers:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("name"), str):
            raise DecisionUnavailable("OpenAI Decisions returned an unnamed answer")
        name = raw["name"]
        if name not in questions or name in answers:
            raise DecisionUnavailable("OpenAI Decisions returned a duplicate or unknown question")
        if raw.get("type") == "refusal":
            raise DecisionUnavailable("OpenAI Decisions refused a choice")
        if raw.get("type") != "choice":
            raise DecisionUnavailable("OpenAI Decisions returned the wrong answer type")
        question = questions[name]
        if not isinstance(question, Mapping) or not isinstance(question.get("criteria"), Mapping):
            raise DecisionUnavailable("OpenAI Decisions local question is invalid")
        criteria = question["criteria"]
        choice = raw.get("choice")
        probabilities = raw.get("probabilities")
        if not isinstance(choice, str) or choice not in criteria or not isinstance(probabilities, list):
            raise DecisionUnavailable("OpenAI Decisions returned an unknown choice")
        values = {}
        try:
            for item in probabilities:
                if not isinstance(item, Mapping) or not isinstance(item.get("value"), str):
                    raise GraphError("invalid choice probability")
                value = item["value"]
                if value not in criteria or value in values:
                    raise GraphError("duplicate or unknown probability option")
                values[value] = probability(item.get("probability"), "probability")
            if set(values) != set(criteria) or not math.isclose(math.fsum(values.values()), 1.0, abs_tol=1e-5, rel_tol=0):
                raise GraphError("incomplete or unnormalized choice probabilities")
            if values[choice] + 1e-8 < max(values.values()):
                raise GraphError("selected choice is not highest probability")
            confidence = probability(raw.get("confidence"), "confidence")
        except (TypeError, GraphError) as exc:
            raise DecisionUnavailable("OpenAI Decisions returned invalid choice probabilities") from exc
        answers[name] = {"type": "choice", "choice": choice, "probabilities": values, "confidence": confidence}
    return {"model": MODEL, "answers": answers}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DecisionUnavailable("OpenAI Decisions redirects are not supported")


def post_decision(packet: Mapping[str, object], *, key: str, timeout_seconds: float) -> object:
    """One bounded POST to the official endpoint; never retry or expose provider bodies."""
    request = urllib.request.Request(ENDPOINT, data=canonical(dict(packet)).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise DecisionUnavailable("OpenAI Decisions HTTP response was not successful")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise DecisionUnavailable("OpenAI Decisions response exceeds byte budget")
            return json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DecisionUnavailable(f"OpenAI Decisions returned HTTP {exc.code}") from None
    except DecisionUnavailable:
        raise
    except Exception:
        raise DecisionUnavailable("OpenAI Decisions transport unavailable") from None
