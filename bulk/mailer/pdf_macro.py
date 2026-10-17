"""PDF Macro Engine.

Fills form fields in PDFs with random strings to create unique hashes.
If no form field exists, injects an invisible text field.
"""
import io
import secrets
import logging
from typing import Optional

logger = logging.getLogger("bulk.pdf")

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False


def fill_pdf_macro(pdf_bytes: bytes, field_name: str = "_uid",
                   value: str = "") -> bytes:
    """Fill a form field in a PDF with a random value.

    If the field doesn't exist, tries to inject one.
    Returns modified PDF bytes with a unique hash.
    """
    if not HAS_PYPDF:
        logger.error("pypdf not installed")
        return pdf_bytes

    if not value:
        value = secrets.token_hex(16)

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        writer = pypdf.PdfWriter()
        writer.append_pages_from_reader(reader)

        if _has_form_fields(reader):
            writer.update_page_form_field_values(
                writer.pages[0], {field_name: value}
            )
        else:
            _inject_hidden_field(writer, field_name, value)

        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    except Exception as exc:
        logger.error("PDF macro failed: %s", exc)
        return pdf_bytes


def _has_form_fields(reader) -> bool:
    try:
        fields = reader.get_fields()
        return bool(fields)
    except Exception:
        return False


def _inject_hidden_field(writer, field_name: str, value: str):
    """Inject a tiny invisible text field into the first page."""
    try:
        from pypdf.generic import (
            DictionaryObject, ArrayObject, NameObject,
            TextStringObject, NumberObject, FloatObject,
        )

        page = writer.pages[0]
        media = page.mediabox

        field = DictionaryObject()
        field.update({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject(field_name),
            NameObject("/V"): TextStringObject(value),
            NameObject("/Rect"): ArrayObject([
                FloatObject(0), FloatObject(0),
                FloatObject(1), FloatObject(1),
            ]),
            NameObject("/F"): NumberObject(6),
        })

        if "/Annots" not in page:
            page[NameObject("/Annots")] = ArrayObject()
        page[NameObject("/Annots")].append(field)

        if "/AcroForm" not in writer._root_object:
            writer._root_object[NameObject("/AcroForm")] = DictionaryObject()
        acro = writer._root_object[NameObject("/AcroForm")]
        if "/Fields" not in acro:
            acro[NameObject("/Fields")] = ArrayObject()
        acro[NameObject("/Fields")].append(field)

    except Exception as exc:
        logger.error("Field injection failed: %s", exc)
