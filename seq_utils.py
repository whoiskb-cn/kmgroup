# -*- coding: utf-8 -*-
import re
from typing import Optional

_NULL_LIKE_VALUES = {"", "none", "nan", "null"}


def _normalize_text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULL_LIKE_VALUES:
        return None
    return text


def normalize_po_no(value) -> Optional[str]:
    return _normalize_text(value)


def normalize_seq_no(value, min_digits: int = 3) -> Optional[str]:
    seq = _normalize_text(value)
    if seq is None:
        return None

    # Excel numeric cells may become strings like "40.0".
    if re.fullmatch(r"[+-]?\d+\.0+", seq):
        seq = str(int(float(seq)))

    if seq.isdigit() and min_digits > 0:
        seq = seq.zfill(min_digits)

    return seq


def po_seq_tuple(po_no, seq_no) -> tuple[str, str]:
    return (normalize_po_no(po_no) or "", normalize_seq_no(seq_no) or "")
