"""Redaction is applied in the SDK and again at the persistence boundary."""

import hashlib
import re
import secrets
from dataclasses import dataclass

from .core import DomainError

MASK = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|authorization|cookie|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|private[_-]?key|credential|client[_-]?secret|^token$|^auth$)",
    re.I,
)
PATTERNS = [
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.S,
    ),
    re.compile(r"\bBearer\s+[^\s\"'<>]+", re.I),
    re.compile(
        r"\b(?:sk-[a-zA-Z0-9_-]{8,}|gh[pousr]_[a-zA-Z0-9]{16,}|"
        r"AKIA[A-Z0-9]{16}|mm_[a-zA-Z0-9_-]{20,})\b"
    ),
    re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|"
        r"authorization)\s*[=:]\s*[\"']?[^\s,;\"']+"
    ),
    re.compile(r"(?i)(?<=://)[^/\s:@]+:[^/\s@]+@"),
]


def redact(value, depth=0):
    if depth > 20:
        raise DomainError("payload_depth", "Payload nesting exceeds 20 levels", 422)
    if isinstance(value, dict):
        return {
            str(k): MASK if SENSITIVE_KEY.search(str(k)) else redact(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in value]
    if isinstance(value, str):
        for pattern in PATTERNS:
            value = pattern.sub(MASK, value)
        return value
    return value


def digest_key(key: str):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key():
    return "mm_" + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    key_id: str
    tenant: str
    grants: dict[str, list[str]]

    def require(self, namespace: str, operation: str):
        if operation not in self.grants.get(namespace, []):
            raise DomainError("forbidden", "Namespace or operation is not authorized", 403)

    def namespaces(self, operation: str):
        return [n for n, operations in self.grants.items() if operation in operations]
