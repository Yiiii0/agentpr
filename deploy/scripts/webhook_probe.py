#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    name: str
    ok: bool
    status_code: int
    response: dict[str, object] | None
    error: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status_code": self.status_code,
            "response": self.response,
            "error": self.error,
        }


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def send_webhook(
    *,
    url: str,
    event: str,
    delivery: str,
    body: bytes,
    secret: str,
    timeout_sec: int,
) -> tuple[int, dict[str, object] | None, str]:
    request = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign(secret, body),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            payload = response.read().decode("utf-8")
            body_obj = json.loads(payload) if payload else None
            return int(response.status), body_obj, ""
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        body_obj: dict[str, object] | None = None
        if payload.strip():
            try:
                body_obj = json.loads(payload)
            except json.JSONDecodeError:
                body_obj = None
        return int(exc.code), body_obj, payload.strip()
    except urllib.error.URLError as exc:
        return 0, None, str(exc)


def run_probe(
    *,
    url: str,
    secret: str,
    timeout_sec: int,
    max_payload_bytes: int,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    base_payload = {
        "repository": {
            "name": "probe-repo",
            "owner": {"login": "probe-owner"},
        },
        "action": "completed",
    }
    body = json.dumps(base_payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    delivery = "agentpr-probe-delivery-1"

    status_1, resp_1, err_1 = send_webhook(
        url=url,
        event="check_run",
        delivery=delivery,
        body=body,
        secret=secret,
        timeout_sec=timeout_sec,
    )
    results.append(
        ProbeResult(
            name="signed_delivery_accept",
            ok=status_1 == 200 and isinstance(resp_1, dict) and bool(resp_1.get("ok", False)),
            status_code=status_1,
            response=resp_1,
            error=err_1,
        )
    )

    status_2, resp_2, err_2 = send_webhook(
        url=url,
        event="check_run",
        delivery=delivery,
        body=body,
        secret=secret,
        timeout_sec=timeout_sec,
    )
    duplicate_flag = bool(resp_2.get("duplicate_delivery")) if isinstance(resp_2, dict) else False
    results.append(
        ProbeResult(
            name="delivery_replay_dedup",
            ok=status_2 == 200 and duplicate_flag,
            status_code=status_2,
            response=resp_2,
            error=err_2,
        )
    )

    oversize_body = b"x" * (max_payload_bytes + 1)
    status_3, resp_3, err_3 = send_webhook(
        url=url,
        event="ping",
        delivery="agentpr-probe-delivery-oversize",
        body=oversize_body,
        secret=secret,
        timeout_sec=timeout_sec,
    )
    results.append(
        ProbeResult(
            name="payload_size_guard",
            ok=status_3 == 413,
            status_code=status_3,
            response=resp_3,
            error=err_3,
        )
    )

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe AgentPR webhook signature/replay/payload guards."
    )
    parser.add_argument("--url", required=True, help="Webhook URL, e.g. http://127.0.0.1:8787/github/webhook")
    parser.add_argument("--secret", required=True, help="Webhook secret configured in AgentPR")
    parser.add_argument("--timeout-sec", type=int, default=10, help="HTTP timeout per request")
    parser.add_argument(
        "--max-payload-bytes",
        type=int,
        default=1048576,
        help="Expected max payload bytes configured in webhook server",
    )
    args = parser.parse_args()

    results = run_probe(
        url=args.url,
        secret=args.secret,
        timeout_sec=max(int(args.timeout_sec), 1),
        max_payload_bytes=max(int(args.max_payload_bytes), 1024),
    )
    ok = all(result.ok for result in results)
    print(
        json.dumps(
            {
                "ok": ok,
                "url": args.url,
                "checks": [result.to_dict() for result in results],
            },
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

