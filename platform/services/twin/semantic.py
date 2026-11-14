"""Semantic IDs (RFC-0031 §3.7, oaap.data.model 0.2 §2.9) -- pure.

A type may carry a `semantic_id`: an IRI or IRDI of a standard (IDTA,
ECLASS, IEC CDD), stored verbatim, or one of OAAP's own in the short form
`oaap-aas:<Submodel>/<major>/<minor>[/<Element>]`. The short form never
names a domain: it is expanded only on the way out, against ONE setting,
so the domain can move without touching a stored row (Jörg, 2026-11-14:
objid.info for now, changeable).
"""
import os
import re

PREFIX = "oaap-aas:"
DEFAULT_BASE = "https://objid.info/oaap/aas"
_OWN_RE = re.compile(r"^oaap-aas:[A-Z][A-Za-z0-9]*/\d+/\d+(/[A-Za-z0-9]+)*$")
_IRI_RE = re.compile(r"^https?://[^\s]+$")
_IRDI_RE = re.compile(r"^[0-9]{4}[-/][0-9A-Za-z/#.\-]+#[0-9A-Za-z\-]+#[0-9]{3}$")


def base():
    """The one place the domain of OAAP's own semantic IDs is set."""
    return (os.environ.get("OAAP_SEMANTIC_BASE") or DEFAULT_BASE).rstrip("/")


def valid(value):
    """An IRI, an IRDI, or OAAP's own short form."""
    v = str(value or "")
    return bool(_OWN_RE.match(v) or _IRI_RE.match(v) or _IRDI_RE.match(v))


def expand(value, base_iri=None):
    """The stored form -> the form a reader gets. Standard IDs unchanged."""
    v = str(value or "")
    if v.startswith(PREFIX):
        return f"{base_iri or base()}/{v[len(PREFIX):]}"
    return v
