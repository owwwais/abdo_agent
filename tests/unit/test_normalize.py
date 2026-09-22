from __future__ import annotations

import pytest

from app.services.imports import decode_csv, read_rows
from app.services.normalize import (
    normalize_cr,
    normalize_email,
    normalize_name,
    normalize_phone,
    website_identifier,
)


def test_arabic_name_normalization() -> None:
    assert normalize_name("شركة مؤسَّسة الأمل للتجارة") == normalize_name("مؤسسه الامل")
    assert normalize_name("عيادة  النُّور") == "عياده النور"
    assert normalize_name("Al-Noor Co. LTD") == "al noor"


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("https://www.Clinic.sa/ar", "domain", "clinic.sa"),
        ("clinic.sa", "domain", "clinic.sa"),
        ("https://salla.sa/store-one", "platform_account", "salla.sa/store-one"),
        ("https://instagram.com/@nakheel", "platform_account", "instagram.com/nakheel"),
        ("https://shop1.myshopify.com", "domain", "shop1.myshopify.com"),
    ],
)
def test_website_identifiers(raw: str, kind: str, value: str) -> None:
    ident, _ = website_identifier(raw)
    assert ident is not None and ident.kind == kind and ident.value == value


def test_multi_tenant_root_and_maps_links_do_not_identify() -> None:
    assert website_identifier("https://salla.sa/")[0] is None
    assert website_identifier("https://myshopify.com")[0] is None
    assert website_identifier("https://maps.app.goo.gl/abc")[0] is None


def test_internal_website_rejected() -> None:
    with pytest.raises(ValueError):
        website_identifier("http://127.0.0.1/")


def test_phone_normalization_requires_known_country() -> None:
    assert normalize_phone("0501234567", "SA") == ("+966501234567", True)
    assert normalize_phone("00966 50 123 4567", None) == ("+966501234567", True)
    value, ok = normalize_phone("0501234567", None)
    assert not ok and value == "raw:0501234567"
    with pytest.raises(ValueError):
        normalize_phone("123", "SA")


def test_email_keeps_local_part_variants() -> None:
    assert normalize_email("Ali.Hassan+sales@Clinic.SA") == "Ali.Hassan+sales@clinic.sa"
    assert normalize_email("alihassan@clinic.sa") != normalize_email("ali.hassan@clinic.sa")
    with pytest.raises(ValueError):
        normalize_email("not-an-email")


def test_cr_number() -> None:
    assert normalize_cr("1010-123-456").value == "1010123456"  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        normalize_cr("12345")


def test_csv_aliases_semicolon_and_cp1256() -> None:
    content = decode_csv("الاسم;الموقع;غير معروف\nعيادة;clinic.sa;x\n".encode("cp1256"))
    rows, ignored = read_rows(content)
    assert rows == [{"name": "عيادة", "website": "clinic.sa"}]
    assert ignored == ["غير معروف"]


def test_csv_without_name_column_is_file_error() -> None:
    from app.api.errors import InvalidInput

    with pytest.raises(InvalidInput) as err:
        read_rows("website,phone\nx.com,1\n")
    assert err.value.code == "missing_name_column"
