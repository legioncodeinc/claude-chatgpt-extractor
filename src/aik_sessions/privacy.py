"""Best-effort secret masking. This is not a general personal-data classifier."""

from __future__ import annotations

import re

from aik_sessions.models import Session

_PATTERNS = (
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", re.IGNORECASE),
    re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s\"'<>]+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
        r"\s*[:=]\s*(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s,;\}\]]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def redact_text(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_session(session: Session) -> Session:
    return session.model_copy(update={
        "title": redact_text(session.title),
        "messages": [message.model_copy(update={"text": redact_text(message.text)})
                     for message in session.messages],
    })
