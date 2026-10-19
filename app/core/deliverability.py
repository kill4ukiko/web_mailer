"""Deliverability diagnostics: Brevo domain auth + DNS (SPF/DKIM/DMARC/MX).

Answers the "why is my mail going to spam?" question in one call by combining
what Brevo thinks of the sending domain with what the public DNS actually
publishes. Every network/DNS failure is captured (never raised) so the endpoint
can always render a useful report.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Brevo publishes DKIM under two selectors for its shared/branded domains.
DKIM_SELECTORS = ("brevo1", "brevo2")
# The SPF include every Brevo-managed domain should reference.
BREVO_SPF_INCLUDE = "include:spf.brevo.com"
_DNS_LIFETIME = 5.0


def _resolver():
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = _DNS_LIFETIME
    resolver.timeout = _DNS_LIFETIME
    # Query public resolvers directly so freshly-added records are visible
    # immediately instead of being masked by a stale local/system DNS cache.
    resolver.nameservers = ["1.1.1.1", "8.8.8.8"]
    return resolver


def _txt_records(domain: str) -> list[str]:
    import dns.resolver

    try:
        answers = _resolver().resolve(domain, "TXT")
    except Exception as exc:  # noqa: BLE001 - NXDOMAIN/timeout are expected
        logger.debug("TXT lookup failed for %s: %s", domain, exc)
        return []
    records: list[str] = []
    for rdata in answers:
        # dnspython exposes multi-chunk TXT via .strings; join to one value.
        try:
            records.append("".join(part.decode() for part in rdata.strings))
        except Exception:  # noqa: BLE001
            records.append(str(rdata).strip('"'))
    return records


def _cname(domain: str) -> str | None:
    try:
        answers = _resolver().resolve(domain, "CNAME")
    except Exception:  # noqa: BLE001
        return None
    vals = [str(r.target).rstrip(".") for r in answers]
    return vals[0] if vals else None


def _mx(domain: str) -> list[str]:
    try:
        answers = _resolver().resolve(domain, "MX")
    except Exception:  # noqa: BLE001
        return []
    return sorted(str(r.exchange).rstrip(".") for r in answers)


def check_dns(domain: str) -> dict[str, Any]:
    """Inspect SPF / DMARC / DKIM / MX records for ``domain``."""
    txt = _txt_records(domain)
    spf = next((r for r in txt if r.lower().startswith("v=spf1")), None)

    dmarc_txt = _txt_records(f"_dmarc.{domain}")
    dmarc = next((r for r in dmarc_txt if r.lower().startswith("v=dmarc1")), None)

    dkim: dict[str, Any] = {}
    for selector in DKIM_SELECTORS:
        host = f"{selector}._domainkey.{domain}"
        target = _cname(host)
        dkim[selector] = {
            "host": host,
            "ok": bool(target and "dkim.brevo.com" in target.lower()),
            "target": target,
        }
    dkim_ok = any(v["ok"] for v in dkim.values())

    mx = _mx(domain)

    return {
        "domain": domain,
        "spf": {
            "ok": bool(spf and BREVO_SPF_INCLUDE in (spf or "").lower()),
            "present": bool(spf),
            "value": spf,
        },
        "dmarc": {
            "ok": bool(dmarc),
            "present": bool(dmarc),
            "value": dmarc,
            "policy_none": bool(dmarc and "p=none" in dmarc.lower()),
        },
        "dkim": {"ok": dkim_ok, "selectors": dkim},
        "mx": {"ok": bool(mx), "records": mx},
    }


def check_brevo_domain(domain: str, settings: Settings | None = None) -> dict[str, Any]:
    """Query Brevo for the domain's verified/authenticated status."""
    settings = settings or get_settings()
    if not settings.brevo_api_key:
        return {"ok": False, "error": "BREVO_API_KEY not configured"}
    url = f"{settings.brevo_api_url.rstrip('/')}/senders/domains/{domain}"
    try:
        resp = requests.get(url, headers={"api-key": settings.brevo_api_key}, timeout=15)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"request failed: {exc}"}
    if resp.status_code == 404:
        return {
            "ok": False,
            "verified": False,
            "authenticated": False,
            "error": "domain not found in Brevo (add it under Senders & Domains)",
        }
    if not resp.ok:
        return {"ok": False, "error": f"Brevo API [{resp.status_code}]: {resp.text[:200]}"}
    data = resp.json() or {}
    return {
        "ok": bool(data.get("verified")),
        "verified": bool(data.get("verified")),
        "authenticated": bool(data.get("authenticated")),
        "raw": {k: data.get(k) for k in ("domain_name", "verified", "authenticated")},
    }


def _recommendations(report: dict[str, Any]) -> list[dict[str, str]]:
    recs: list[dict[str, str]] = []
    brevo = report["brevo"]
    dns = report["dns"]

    if not brevo.get("ok"):
        recs.append({
            "level": "error",
            "message": "Sending domain is not verified in Brevo. "
                       "Add it under Senders & Domains and complete verification.",
        })
    if not dns["dkim"]["ok"]:
        recs.append({
            "level": "error",
            "message": "No Brevo DKIM record found. Add the brevo1/brevo2 "
                       "CNAMEs (or the DKIM TXT) from Brevo. DKIM is required for "
                       "Gmail/Outlook inbox placement.",
        })
    if not dns["spf"]["present"]:
        recs.append({
            "level": "warning",
            "message": f"No SPF record. Recommended: v=spf1 {BREVO_SPF_INCLUDE} ~all",
        })
    elif not dns["spf"]["ok"]:
        recs.append({
            "level": "warning",
            "message": f"SPF exists but is missing {BREVO_SPF_INCLUDE}.",
        })
    if not dns["dmarc"]["present"]:
        recs.append({
            "level": "warning",
            "message": "No DMARC record. Start with: v=DMARC1; p=none; "
                       "rua=mailto:dmarc@<your-domain>",
        })
    if not dns["mx"]["ok"]:
        recs.append({
            "level": "info",
            "message": "No MX record. This is optional if you only send (outbound-only) "
                       "and don't need to receive replies on this domain. Add an MX "
                       "record only if you want replies delivered to a mailbox.",
        })
    return recs


def full_report(domain: str, settings: Settings | None = None) -> dict[str, Any]:
    """Combined Brevo + DNS deliverability report with actionable advice."""
    report = {
        "domain": domain,
        "brevo": check_brevo_domain(domain, settings),
        "dns": check_dns(domain),
    }
    report["recommendations"] = _recommendations(report)
    return report


# --------------------------------------------------------------------------- #
# Reputation analytics (aggregate sending outcomes + suppression signals)
# --------------------------------------------------------------------------- #
_TOTAL_FIELDS = ("sent", "delivered", "opens", "clicks", "bounces", "complaints", "unsubscribes")


def _domain_of(email: str | None) -> str:
    return (email or "").rsplit("@", 1)[-1].lower()


def reputation_summary(supabase, top_n: int = 10) -> dict[str, Any]:
    """Aggregate reputation signals across all campaigns and contacts.

    Best-effort and never raising: returns zeroed totals if the database is
    unavailable. Rates are percentages of sent volume. ``top_bounce_domains``
    surfaces the domains producing the most suppressed (bounced) addresses so an
    operator can spot a bad source before it damages the sender reputation.
    """
    totals = {field: 0 for field in _TOTAL_FIELDS}
    campaigns: list[dict] = []
    contacts: list[dict] = []

    try:
        campaigns = (
            supabase.client.table("campaigns")
            .select("id,name,status,stats,created_at,finished_at")
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("reputation_summary campaigns query failed: %s", exc)

    for campaign in campaigns:
        stats = campaign.get("stats") or {}
        for field in _TOTAL_FIELDS:
            totals[field] += int(stats.get(field, 0) or 0)

    sent = totals["sent"]
    rates = {
        field: (round(totals[field] / sent * 100, 3) if sent else 0.0)
        for field in _TOTAL_FIELDS
        if field != "sent"
    }

    try:
        contacts = (
            supabase.client.table("contacts").select("email,status").execute().data or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("reputation_summary contacts query failed: %s", exc)

    suppression: dict[str, int] = {}
    domain_bounces: dict[str, int] = {}
    for contact in contacts:
        status = contact.get("status") or "active"
        suppression[status] = suppression.get(status, 0) + 1
        if status in ("bounced_hard", "bounced_soft"):
            domain = _domain_of(contact.get("email"))
            if domain:
                domain_bounces[domain] = domain_bounces.get(domain, 0) + 1

    top_bounce_domains = [
        {"domain": domain, "count": count}
        for domain, count in sorted(domain_bounces.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    ]
    # Contacts known to be reusable (cleanly delivered / never failed).
    deliverable = suppression.get("active", 0)

    return {
        "totals": totals,
        "rates": rates,
        "suppression": suppression,
        "deliverable_contacts": deliverable,
        "top_bounce_domains": top_bounce_domains,
    }
