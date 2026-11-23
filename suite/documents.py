"""Versioned document helpers. Every write that changes content adds a version."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DocKind, Document, DocumentVersion, ScopeType, VersionSource


@dataclass(frozen=True)
class DocRef:
    """A specific document version, as recorded in context manifests."""

    document_id: int
    kind: str
    key: str
    version: int
    content: str


def find(
    session: Session, scope_type: ScopeType, scope_id: int, kind: DocKind, key: str = "main"
) -> Optional[Document]:
    return session.scalar(
        select(Document).where(
            Document.scope_type == scope_type,
            Document.scope_id == scope_id,
            Document.kind == kind,
            Document.key == key,
        )
    )


def list_docs(
    session: Session, scope_type: ScopeType, scope_id: int, kind: DocKind | None = None
) -> list[Document]:
    q = select(Document).where(
        Document.scope_type == scope_type, Document.scope_id == scope_id
    )
    if kind is not None:
        q = q.where(Document.kind == kind)
    return list(session.scalars(q.order_by(Document.kind, Document.key)))


def current(session: Session, doc: Document | None) -> Optional[DocRef]:
    """Latest version of a document, or None if it has no content yet."""
    if doc is None or doc.current_version == 0:
        return None
    ver = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == doc.id,
            DocumentVersion.version == doc.current_version,
        )
    )
    if ver is None:
        return None
    return DocRef(doc.id, doc.kind.value, doc.key, ver.version, ver.content)


def read(
    session: Session, scope_type: ScopeType, scope_id: int, kind: DocKind, key: str = "main"
) -> Optional[DocRef]:
    return current(session, find(session, scope_type, scope_id, kind, key))


def write(
    session: Session,
    *,
    user_id: int,
    scope_type: ScopeType,
    scope_id: int,
    kind: DocKind,
    key: str = "main",
    content: str,
    source: VersionSource,
    note: str | None = None,
) -> tuple[DocRef, bool]:
    """Save content as a new version. Returns (ref, changed).

    Writing identical content is a no-op, so re-importing a package does not
    pile up empty versions.
    """
    content = content.strip() + "\n" if content.strip() else ""
    doc = find(session, scope_type, scope_id, kind, key)
    if doc is None:
        doc = Document(
            user_id=user_id,
            scope_type=scope_type,
            scope_id=scope_id,
            kind=kind,
            key=key,
            current_version=0,
        )
        session.add(doc)
        session.flush()
    else:
        existing = current(session, doc)
        if existing is not None and existing.content == content:
            return existing, False

    new_version = doc.current_version + 1
    session.add(
        DocumentVersion(
            document_id=doc.id,
            version=new_version,
            content=content,
            source=source,
            note=note,
        )
    )
    doc.current_version = new_version
    session.flush()
    return DocRef(doc.id, kind.value, key, new_version, content), True
