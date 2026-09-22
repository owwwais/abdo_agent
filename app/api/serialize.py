"""تحويل السجلات إلى JSON للواجهة البرمجية. قيم جهات الاتصال تُقنّع في القوائم."""

from __future__ import annotations

from typing import Any

from app.db.models import (
    Company,
    ImportBatch,
    ImportRow,
    Job,
    Product,
    Segment,
    Source,
    SourceCheck,
)


def _iso(v: Any) -> Any:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def segment_out(s: Segment) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "name": s.name,
        "description": s.description,
        "fit_rules": s.fit_rules,
        "exclusion_rules": s.exclusion_rules,
        "status": s.status,
        "version": s.version,
    }


def product_out(p: Product, segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out = {
        "id": str(p.id),
        "name": p.name,
        "summary": p.summary,
        "problem": p.problem,
        "capabilities": p.capabilities,
        "unavailable_capabilities": p.unavailable_capabilities,
        "fit_signals": p.fit_signals,
        "exclusions": p.exclusions,
        "product_url": p.product_url,
        "demo_url": p.demo_url,
        "price_status": p.price_status,
        "price_text": p.price_text,
        "priority": p.priority,
        "status": p.status,
        "version": p.version,
        "is_demo_data": p.is_demo_data,
        "updated_at": _iso(p.updated_at),
    }
    if segments is not None:
        out["segments"] = segments
    return out


def check_out(c: SourceCheck | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "id": str(c.id),
        "status": c.status,
        "config_version": c.config_version,
        "summary": c.sample_summary,
        "fields_found": c.allowed_fields,
        "fields_missing": c.missing_fields,
        "errors": c.errors,
        "request_count": c.request_count,
        "bytes_fetched": c.bytes_fetched,
        "duration_ms": c.duration_ms,
        "cost": None
        if c.cost_amount is None
        else {"amount": str(c.cost_amount), "currency": c.cost_currency},
        "checked_at": _iso(c.checked_at),
    }


def source_out(s: Source, check: SourceCheck | None = None) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "name": s.name,
        "url": s.url,
        "kind": s.kind,
        "connector_key": s.connector_key,
        "access_mode": s.access_mode,
        "allowed_hosts": s.allowed_hosts,
        "allowed_paths": s.allowed_paths,
        "limits": {
            "max_pages": s.max_pages,
            "max_records": s.max_records,
            "max_requests": s.max_requests,
            "timeout_seconds": s.timeout_seconds,
        },
        "refresh_interval_hours": s.refresh_interval_hours,
        "credential_ref": s.credential_ref,
        "store_raw": s.store_raw,
        "retention_days": s.retention_days,
        "status": s.status,
        "status_reason": s.status_reason,
        "version": s.config_version,
        "policy_version": s.policy_version,
        "policy_confirmed_at": _iso(s.policy_confirmed_at),
        "last_success_at": _iso(s.last_success_at),
        "last_error": s.last_error,
        "latest_check": check_out(check),
    }


def job_out(j: Job) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "kind": j.kind,
        "status": j.status,
        "attempts": j.attempts,
        "run_after": _iso(j.run_after),
        "last_error": j.last_error,
    }


def company_out(c: Company) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "display_name": c.display_name,
        "domain": c.domain,
        "sector": c.sector,
        "region": c.region,
        "city": c.city,
        "country": c.country,
        "status": c.status,
        "segment_id": str(c.segment_id) if c.segment_id else None,
        "is_demo_data": c.is_demo_data,
        "created_at": _iso(c.created_at),
    }


def mask(channel: str, value: str) -> str:
    if channel == "email" and "@" in value:
        local, domain = value.split("@", 1)
        return (local[:2] + "•••@" + domain) if len(local) > 2 else "•••@" + domain
    return value[:4] + "•••" + value[-2:] if len(value) > 6 else "•••"


def batch_out(b: ImportBatch, rows: list[ImportRow] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(b.id),
        "kind": b.kind,
        "status": b.status,
        "filename": b.filename,
        "row_count": b.row_count,
        "error_count": b.error_count,
        "summary": b.summary,
        "created_at": _iso(b.created_at),
        "committed_at": _iso(b.committed_at),
    }
    if rows is not None:
        out["rows"] = [
            {
                "row_number": r.row_number,
                "display_name": r.display_name,
                "action": r.action,
                "errors": r.errors,
                "match": r.match,
                "company_id": str(r.result_company_id) if r.result_company_id else None,
            }
            for r in rows
        ]
    return out
