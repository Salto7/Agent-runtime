"""Open Asset Model (OAM)-aligned vocabulary for the engagement graph.

The graph stays *open*: producers may use any type or relation slug and it is
stored and rendered. This module adds the structure a mature asset graph needs
on top of that, modelled on OWASP Amass' Open Asset Model:

* **Asset classes** — which OAM asset type a stored ``(type, value)`` is, plus a
  display *group* (DNS, network, web, organization, …) used for layout/colour.
* **Relation registry** — the expected ``source class -> label -> target class``
  triples (``a_record``, ``port``, ``contains``, ``announces``, …). Unknown
  labels are accepted as ``custom``; known labels between unexpected classes
  are flagged ``unexpected`` rather than rejected.

Identity keys (``asset_graph.asset_key``) are deliberately *not* derived from
this module, so reclassifying never changes stored node ids.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.engagement.target_shapes import detect_shape, sanitize_label


@dataclass(frozen=True)
class AssetClass:
    slug: str  # stable class id used in API payloads
    oam: str  # Open Asset Model asset type
    label: str  # human label
    group: str  # display family
    aliases: tuple[str, ...] = ()


# Display groups, in legend order.
GROUPS: tuple[tuple[str, str], ...] = (
    ("dns", "DNS"),
    ("network", "Network"),
    ("service", "Services"),
    ("web", "Web"),
    ("org", "Organizations & people"),
    ("identity", "Identities"),
    ("registry", "Registry"),
    ("software", "Software"),
    ("file", "Files & artifacts"),
    ("location", "Locations"),
    ("other", "Other"),
)

CLASSES: tuple[AssetClass, ...] = (
    AssetClass("fqdn", "FQDN", "FQDN", "dns",
               ("host", "hostname", "domain", "fqdn", "subdomain", "dns", "dns_name", "site")),
    AssetClass("ip", "IPAddress", "IP address", "network",
               ("ip", "ip_address", "ipv4", "ipv6", "address")),
    AssetClass("netblock", "Netblock", "Netblock", "network",
               ("netblock", "cidr", "subnet", "network", "net", "range")),
    AssetClass("asn", "AutonomousSystem", "Autonomous system", "network",
               ("asn", "as", "autonomous_system")),
    AssetClass("service", "Service", "Service", "service",
               ("service", "port", "endpoint", "socket", "listener", "banner")),
    AssetClass("url", "URL", "URL", "web", ("url", "uri", "link", "webpage", "page")),
    AssetClass("certificate", "TLSCertificate", "TLS certificate", "web",
               ("certificate", "cert", "tls", "tls_certificate", "ssl", "x509")),
    AssetClass("organization", "Organization", "Organization", "org",
               ("organization", "organisation", "company", "org", "vendor", "brand",
                "asset", "name", "business", "subsidiary")),
    AssetClass("person", "Person", "Person", "org",
               ("person", "employee", "individual", "people", "staff")),
    AssetClass("contact", "ContactRecord", "Contact record", "org",
               ("contact", "contact_record")),
    AssetClass("email", "Identifier (email)", "Email address", "identity",
               ("email", "email_address", "mail", "mailbox")),
    AssetClass("phone", "Phone", "Phone", "identity", ("phone", "telephone", "msisdn")),
    AssetClass("account", "Account", "Account", "identity",
               ("account", "username", "handle", "login", "user", "profile", "social")),
    AssetClass("identifier", "Identifier", "Identifier", "identity",
               ("identifier", "id", "token", "key", "secret")),
    AssetClass("hash", "Identifier (hash)", "Hash", "identity",
               ("hash", "digest", "sha256", "sha1", "md5", "fingerprint")),
    AssetClass("domain_record", "DomainRecord", "Domain registration", "registry",
               ("domain_record", "whois", "registration", "rdap")),
    AssetClass("ipnet_record", "IPNetRecord", "Network registration", "registry",
               ("ipnet_record", "ipnet", "netrange", "inetnum")),
    AssetClass("autnum_record", "AutnumRecord", "ASN registration", "registry",
               ("autnum_record", "autnum", "aut_num")),
    AssetClass("product", "Product", "Product", "software",
               ("product", "software", "package", "library", "technology", "tech",
                "framework", "application", "app", "cpe")),
    AssetClass("release", "ProductRelease", "Release", "software",
               ("release", "version", "build", "product_release")),
    AssetClass("repository", "Repository", "Repository", "software",
               ("repository", "repo", "git", "source")),
    AssetClass("file", "File", "File", "file",
               ("file", "path", "artifact", "document", "binary", "sample", "malware",
                "pcap", "dump", "attachment", "evidence")),
    AssetClass("location", "Location", "Location", "location",
               ("location", "geo", "city", "country", "region", "place")),
    AssetClass("funds", "FundsTransfer", "Funds transfer", "other",
               ("funds_transfer", "payment", "transaction", "wallet")),
    AssetClass("other", "Other", "Other", "other", ()),
)

_BY_SLUG = {c.slug: c for c in CLASSES}
_BY_ALIAS: dict[str, AssetClass] = {}
for _c in CLASSES:
    _BY_ALIAS[_c.slug] = _c
    for _a in _c.aliases:
        _BY_ALIAS.setdefault(_a, _c)

# detect_shape() result → class slug (value shapes are authoritative).
_SHAPE_CLASS = {
    "ip": "ip",
    "netblock": "netblock",
    "url": "url",
    "email": "email",
    "asn": "asn",
    "service": "service",
    "hash": "hash",
}


def class_by_slug(slug: str) -> AssetClass:
    return _BY_SLUG.get(slug, _BY_SLUG["other"])


def classify(typ: str, value: str = "") -> AssetClass:
    """OAM class for a stored asset.

    The value's standards shape wins (an IP is an IP whatever the producer
    called it); otherwise the producer's type slug is resolved through the
    alias table; unknown slugs are ``other`` but keep their raw slug upstream.
    """
    shape = detect_shape(value or "")
    if shape in _SHAPE_CLASS:
        return _BY_SLUG[_SHAPE_CLASS[shape]]
    slug = sanitize_label(typ, default="")
    return _BY_ALIAS.get(slug, _BY_SLUG["other"])


# ── Relations ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RelationSpec:
    label: str
    title: str
    sources: frozenset[str]  # class slugs; empty = any
    targets: frozenset[str]
    oam: bool = True  # defined by OAM (vs. platform-specific)


def _spec(label: str, title: str, src: str, dst: str, *, oam: bool = True) -> RelationSpec:
    return RelationSpec(
        label, title, frozenset(src.split()), frozenset(dst.split()), oam
    )


_ANY = ""
RELATIONS: dict[str, RelationSpec] = {
    r.label: r
    for r in (
        # DNS
        _spec("a_record", "A record", "fqdn", "ip"),
        _spec("aaaa_record", "AAAA record", "fqdn", "ip"),
        _spec("cname_record", "CNAME", "fqdn", "fqdn"),
        _spec("ns_record", "NS record", "fqdn", "fqdn"),
        _spec("mx_record", "MX record", "fqdn", "fqdn"),
        _spec("srv_record", "SRV record", "fqdn", "fqdn"),
        _spec("ptr_record", "PTR record", "ip fqdn", "fqdn"),
        _spec("txt_record", "TXT record", "fqdn", _ANY),
        _spec("node", "Subdomain", "fqdn", "fqdn"),
        # Network
        _spec("port", "Port", "ip fqdn", "service"),
        _spec("contains", "Contains", "netblock", "ip netblock"),
        _spec("announces", "Announces", "asn", "netblock"),
        # Registration & ownership
        _spec("registration", "Registration", "fqdn netblock asn ip",
              "domain_record ipnet_record autnum_record"),
        _spec("managed_by", "Managed by", _ANY, "organization person contact"),
        _spec("associated_with", "Associated with", _ANY, _ANY),
        _spec("legal_name", "Legal name", "domain_record ipnet_record autnum_record",
              "organization"),
        _spec("contact", "Contact", "organization person domain_record",
              "email phone contact"),
        _spec("certificate", "Certificate", "service url fqdn", "certificate"),
        _spec("subject", "Subject", "certificate", "fqdn"),
        _spec("hosts", "Hosts", "ip fqdn service", "url"),
        _spec("owns", "Owns", "organization person",
              "fqdn netblock asn account product repository"),
        _spec("member_of", "Member of", "person account", "organization"),
        _spec("uses", "Uses", "service url fqdn organization", "product release"),
        _spec("located_at", "Located at", "organization person ip netblock", "location"),
        # platform-specific (kept from the first graph version)
        _spec("ip_address", "Resolves to", "url", "ip", oam=False),
        _spec("domain", "On domain", "url", "fqdn", oam=False),
        _spec("same_file", "Same file", "file", "file", oam=False),
        _spec("hashes", "Hashes", "hash", "file", oam=False),
    )
}


def relation_title(label: str) -> str:
    spec = RELATIONS.get(label)
    return spec.title if spec else label.replace("_", " ").capitalize()


def relation_status(rel: str, source: AssetClass, target: AssetClass) -> str:
    """``ok`` | ``unexpected`` (known label, odd endpoints) | ``custom``."""
    spec = RELATIONS.get(rel)
    if spec is None:
        return "custom"
    # ``other`` endpoints are unclassified, not wrong.
    if spec.sources and source.slug not in spec.sources and source.slug != "other":
        return "unexpected"
    if spec.targets and target.slug not in spec.targets and target.slug != "other":
        return "unexpected"
    return "ok"


def parent_fqdn_candidates(name: str) -> list[str]:
    """Ancestors of an FQDN, nearest first (``a.b.c.com`` → ``b.c.com``, ``c.com``).

    Stops before a bare single label; callers link only to ancestors that
    already exist in the graph (no invented nodes).
    """
    labels = [p for p in (name or "").strip().lower().rstrip(".").split(".") if p]
    return [".".join(labels[i:]) for i in range(1, len(labels) - 1)]
