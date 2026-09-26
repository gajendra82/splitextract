"""Format-aware ground truth built from PDF word positions.

Column roles come from the printed header, not from the production parser
and not from an LLM. A column that is not printed stays null.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from qa.pdf_source import PageSource, Word, load_pdf_pages

CANONICAL_FIELDS = (
    "product_code",
    "product_name",
    "opening_qty",
    "opening_value",
    "receipts_qty",
    "receipts_value",
    "sales_qty",
    "sales_value",
    "closing_qty",
    "closing_value",
)

_DASH = {"-", "—", "–", "--", "−"}
_ROLE_EXACT = {
    "qty",
    "quantity",
    "qnty",
    "value",
    "val",
    "amt",
    "amount",
    "mount",
    "balance",
    "rate",
    "price",
}


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _is_role_word(text: str) -> bool:
    return _compact(text) in _ROLE_EXACT


@dataclass
class Column:
    field: str
    x: float
    x0: float
    x1: float


@dataclass
class _Cluster:
    text: str
    x0: float
    x1: float

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2.0


def _family(label: str) -> str:
    """Map a header label (role suffix already removed) to a column family."""
    s = label
    if not s:
        return "skip"
    if s in {"salesretfree", "salesreturnfree", "saleretfree"}:
        return "sales_return_free"
    if s in {"salesfree", "salefree"} or s.startswith("salesfree"):
        return "sales_free"
    if s in {"salesret", "salesreturn", "saleret", "salereturn"} or s.startswith("saleret"):
        return "sales_return"
    if s in {"purchasertn", "purchasereturn", "purreturn", "prn"}:
        return "purchase_return"
    if s in {"purchasefree", "purfree"}:
        return "purchase_free"
    if s in {"expdmg", "expdamage", "expirydamage"}:
        return "exp_damage"
    if s in {"orec", "otherreceipt"}:
        return "other_receipt"
    if s in {"oiss", "otherissue"}:
        return "other_issue"
    if s in {
        "opening",
        "op",
        "opstk",
        "opstock",
        "opbal",
        "opqty",
        "open",
    } or s.startswith("opening") or s.startswith("opstk") or s.startswith("opbal"):
        return "opening"
    if s in {"receipt", "receipts", "recevied", "received", "purchase", "pur", "inward"}:
        return "receipts"
    if s.startswith("receipt") or s.startswith("purchase") or s.startswith("inward"):
        return "receipts"
    if s in {"issue", "issues", "outward"}:
        return "sales"
    if s in {"sales", "sale"}:
        return "sales"
    if s in {
        "closing",
        "cl",
        "clstk",
        "clstock",
        "clos",
        "closstock",
        "clsqty",
        "cqty",
    } or s.startswith("closing") or s.startswith("clstk") or s.startswith("clos"):
        return "closing"
    if s in {"total", "tot"}:
        return "total"
    if s in {"product", "item", "description", "particular", "particulars", "name"}:
        return "product"
    if "description" in s or "particular" in s or s.startswith("product") or s.startswith("item"):
        return "product"
    if s in {"packing", "pack", "strength", "packsize"}:
        return "packing"
    if s in {"code", "itemcode", "productcode"}:
        return "code"
    if s in {"rate", "mrp", "ptr"}:
        return "rate"
    if s in {"dump"}:
        return "dump"
    if s in {"sr", "sno", "slno", "no"}:
        return "skip"
    return "skip"


def _strip_embedded_role(label: str) -> Tuple[str, Optional[str]]:
    for suffix, role in (
        ("quantity", "qty"),
        ("amount", "value"),
        ("value", "value"),
        ("balance", "qty"),
        ("qty", "qty"),
        ("amt", "value"),
        ("val", "value"),
    ):
        if label.endswith(suffix) and len(label) > len(suffix):
            return label[: -len(suffix)], role
    return label, None


def _role_of(text: str) -> Optional[str]:
    token = _compact(text)
    if token in {"qty", "quantity", "qnty", "balance"}:
        return "qty"
    if token in {"value", "val", "amt", "amount", "mount", "price"}:
        return "value"
    if token in {"rate"}:
        return "rate"
    return None


def field_from(parent_text: str, sub_text: str = "") -> str:
    """Map a header cluster, plus an optional Qty/Value sub-label, to a field."""
    raw = (parent_text or "").strip().lower()
    if raw in {"#", "sr", "sr.", "s.no", "s.no.", "sno", "no", "no."}:
        return "_skip"
    label = _compact(parent_text)
    label, embedded = _strip_embedded_role(label)
    fam = _family(label)
    role = _role_of(sub_text) or embedded
    if fam == "product":
        return "product_name"
    if fam == "code":
        return "product_code"
    if fam == "packing":
        return "packing"
    if fam == "rate" or role == "rate":
        if fam in {"opening", "receipts", "sales", "closing"}:
            pass
        else:
            return "unit_rate"
    if fam == "skip":
        return "_skip"
    if fam == "total":
        return "total_stock"
    if fam == "dump":
        return "dump_qty"
    if fam == "exp_damage":
        return "exp_damage"
    if fam == "sales_free":
        return "sales_free"
    if fam == "sales_return":
        return "sales_return"
    if fam == "sales_return_free":
        return "sales_return_free"
    if fam == "purchase_return":
        return "purchase_return"
    if fam == "purchase_free":
        return "purchase_free"
    if fam == "other_receipt":
        return "other_receipt_value" if role == "value" else "other_receipt_qty"
    if fam == "other_issue":
        return "other_issue_value" if role == "value" else "other_issue_qty"
    if fam in {"opening", "receipts", "sales", "closing"}:
        kind = "value" if role == "value" else "qty"
        prefix = {
            "opening": "opening",
            "receipts": "receipts",
            "sales": "sales",
            "closing": "closing",
        }[fam]
        return f"{prefix}_{kind}"
    return "_skip"


_HEADER_CONTINUATION = {
    "name",
    "qty",
    "quantity",
    "qnty",
    "val",
    "value",
    "description",
    "dt",
    "amt",
    "amount",
    "bal",
    "balance",
    "stock",
    "no",
    "price",
    "as",
    "on",
}


def _joins_header(previous: str, nxt: str, gap: float) -> bool:
    """Join 'Product'+'Name' or 'Op.'+'Qty', but not neighbouring columns."""
    if gap > 12:
        return False
    token = _compact(nxt)
    prev = _compact(previous)
    if token in _HEADER_CONTINUATION:
        return True
    if token == "free" and "sale" in prev:
        return True
    if token in {"ret", "rtn", "return"} and ("sale" in prev or "pur" in prev):
        return True
    return False


def _cluster_line(words: Sequence[Word]) -> List[_Cluster]:
    ordered = sorted(words, key=lambda w: w[0])
    clusters: List[_Cluster] = []
    for word in ordered:
        gap = word[0] - clusters[-1].x1 if clusters else 99
        if clusters and _joins_header(clusters[-1].text, word[4], gap):
            clusters[-1].text = (clusters[-1].text + " " + word[4]).strip()
            clusters[-1].x1 = max(clusters[-1].x1, word[2])
            clusters[-1].x0 = min(clusters[-1].x0, word[0])
        else:
            clusters.append(_Cluster(text=word[4], x0=word[0], x1=word[2]))
    return clusters


def _row_score(words: Sequence[Word]) -> int:
    texts = [w[4] for w in words]
    compacts = [_compact(t) for t in texts]
    numeric = sum(1 for t in texts if re.fullmatch(r"-?\d+(?:\.\d+)?", t.replace(",", "")))
    if numeric >= 3:
        return 0
    score = 0
    blob = " ".join(compacts)
    if any(c in {"product", "item", "description", "particular", "particulars"} or c.startswith("product") or c.startswith("item") for c in compacts):
        score += 4
    if "productname" in blob or "itemname" in blob or "itemdescription" in blob:
        score += 2
    metric_bits = (
        "opening",
        "opstk",
        "opstock",
        "opbal",
        "opqty",
        "receipt",
        "recevied",
        "purchase",
        "inward",
        "sales",
        "issue",
        "outward",
        "closing",
        "clstk",
        "clstock",
        "closstock",
    )
    hits = 0
    for bit in metric_bits:
        if any(c == bit or c.startswith(bit) for c in compacts):
            hits += 1
    # "Op." alone
    if any(c in {"op", "cl", "pur"} for c in compacts):
        hits += 1
    score += min(hits, 4) * 2
    return score


def _is_sub_row(words: Sequence[Word]) -> bool:
    if not words:
        return False
    alpha = [w for w in words if re.search(r"[A-Za-z]", w[4])]
    if not alpha:
        return False
    roles = sum(1 for w in alpha if _is_role_word(w[4]) or _compact(w[4]) in {"as", "on"})
    numeric = sum(1 for w in words if re.search(r"\d", w[4]) and not _is_role_word(w[4]))
    if numeric >= 2:
        return False
    return roles / max(len(alpha), 1) >= 0.34


def _nearest_cluster(word: Word, clusters: Sequence[_Cluster]) -> Optional[_Cluster]:
    cx = (word[0] + word[2]) / 2.0
    best = None
    best_dist = 1e9
    for cluster in clusters:
        if cluster.x0 - 12 <= cx <= cluster.x1 + 14:
            dist = abs(cx - cluster.center)
        else:
            dist = abs(cx - cluster.center)
        if dist < best_dist:
            best = cluster
            best_dist = dist
    if best is None or best_dist > 46:
        return None
    return best


def build_columns(parent: Sequence[Word], sub: Optional[Sequence[Word]] = None) -> List[Column]:
    clusters = _cluster_line(parent)
    columns: List[Column] = []
    used = set()
    sub_words = list(sub or [])
    for word in sub_words:
        if not _is_role_word(word[4]):
            continue
        cluster = _nearest_cluster(word, clusters)
        if cluster is None:
            field = field_from(word[4], "")
        else:
            field = field_from(cluster.text, word[4])
            used.add(id(cluster))
        cx = (word[0] + word[2]) / 2.0
        columns.append(Column(field=field, x=cx, x0=word[0], x1=word[2]))
    for cluster in clusters:
        if id(cluster) in used:
            continue
        field = field_from(cluster.text, "")
        columns.append(
            Column(field=field, x=cluster.center, x0=cluster.x0, x1=cluster.x1)
        )
    for word in sub_words:
        if _is_role_word(word[4]):
            continue
        cx = (word[0] + word[2]) / 2.0
        if any(col.x0 - 4 <= cx <= col.x1 + 4 for col in columns):
            continue
        field = field_from(word[4], "")
        columns.append(Column(field=field, x=cx, x0=word[0], x1=word[2]))
    columns.sort(key=lambda c: c.x)
    # Drop duplicate fields that landed on the same x (keep the tighter one).
    deduped: List[Column] = []
    for col in columns:
        if deduped and col.field == deduped[-1].field and abs(col.x - deduped[-1].x) < 8:
            continue
        deduped.append(col)
    # "Issues" and a following "Issue" are different printed columns.
    # The left one is sales. Later copies must not steal Closing.
    seen_sales = False
    for col in deduped:
        if col.field != "sales_qty":
            continue
        if not seen_sales:
            seen_sales = True
            continue
        col.field = "other_issue_qty"
    return deduped


def _cluster_rows(words: Sequence[Word]) -> List[dict]:
    if not words:
        return []
    heights = sorted((w[3] - w[1]) for w in words if w[3] > w[1])
    median_h = heights[len(heights) // 2] if heights else 8.0
    tol = max(3.0, min(5.5, median_h * 0.5))
    ordered = sorted(words, key=lambda w: ((w[1] + w[3]) / 2.0, w[0]))
    rows: List[dict] = []
    for word in ordered:
        cy = (word[1] + word[3]) / 2.0
        if not rows or abs(cy - rows[-1]["anchor"]) > tol:
            rows.append({"anchor": cy, "words": [word]})
        else:
            rows[-1]["words"].append(word)
    for row in rows:
        row["words"].sort(key=lambda w: w[0])
        row["cy"] = sum((w[1] + w[3]) / 2.0 for w in row["words"]) / len(row["words"])
    return rows


def _find_header(rows: Sequence[dict]) -> Tuple[Optional[int], Optional[int]]:
    best_i = None
    best_score = 0
    for i, row in enumerate(rows[:40]):
        score = _row_score(row["words"])
        if score > best_score:
            best_score = score
            best_i = i
    if best_i is None or best_score < 6:
        return None, None
    sub_i = None
    if best_i + 1 < len(rows):
        gap = rows[best_i + 1]["cy"] - rows[best_i]["cy"]
        if 0 < gap < 18 and _is_sub_row(rows[best_i + 1]["words"]):
            sub_i = best_i + 1
    return best_i, sub_i


def parse_number(token: str) -> Optional[float]:
    """Parse one cell. A printed dash is 0. A non-numeric token is unavailable."""
    if token is None:
        return None
    text = str(token).strip()
    if not text:
        return None
    if text in _DASH or text.upper() in {"NA", "N/A", "NIL"}:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = text.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if negative and value:
        value = -abs(value)
    return value


_PACK_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?\s*(?:ML|MG|GM|GR|G|KG|L|TAB|TABS|CAP|CAPS|'S|S)|"
    r"\d+\s*'S|\d+'S)$",
    re.I,
)


def _peel_packing(tokens: List[str]) -> Tuple[str, Optional[str]]:
    """Move a trailing pack token out of the name. Keep 'LIV 52 TAB' intact."""
    pack: List[str] = []
    while tokens:
        last = tokens[-1]
        single = bool(
            re.fullmatch(
                r"\d+(?:\.\d+)?(?:ML|MG|GM|GR|TAB|TABS|CAP|CAPS|'S|S)",
                last,
                re.I,
            )
        )
        if single or _PACK_RE.match(last.replace(" ", "")):
            pack.insert(0, tokens.pop())
            continue
        if (
            len(tokens) >= 2
            and re.fullmatch(r"ML|MG|GM|GR", tokens[-1], re.I)
            and re.fullmatch(r"\d+(?:\.\d+)?", tokens[-2])
        ):
            pack.insert(0, tokens.pop())
            pack.insert(0, tokens.pop())
            continue
        break
    name = re.sub(r"\s+", " ", " ".join(tokens)).strip(" -_|")
    packing = re.sub(r"\s+", " ", " ".join(pack)).strip() or None
    return name, packing


_JUNK_NAME = re.compile(
    r"^(?:page\b|continued\b|grand\s+total\b|sub\s*total\b|total\b|"
    r"phone\b|gstin\b|e-?mail\b|group\s+wise\b|product\s+name\b|"
    r"stock\s*&?\s*sales\b|sales\s*&\s*stock\b)",
    re.I,
)


def _junk_name(name: str) -> bool:
    text = (name or "").strip()
    if len(re.sub(r"[^A-Za-z]", "", text)) < 2:
        return True
    if _JUNK_NAME.search(text):
        return True
    if re.search(r"page\s*\d+|/\d{1,2}/\d{4}|as on\b", text, re.I):
        return True
    if re.fullmatch(r"[\d./:\-\s]+", text):
        return True
    return False


def _column_anchor(col: Column) -> float:
    # Quantities are usually right-aligned under the header, so the header's
    # right edge is a better anchor than its centre.
    if col.field in {"product_name", "product_code"}:
        return (col.x0 + col.x1) / 2.0
    return col.x1


def _assign_row(words: Sequence[Word], columns: Sequence[Column]) -> Dict[str, List[str]]:
    cells: Dict[str, List[str]] = {}
    ordered = sorted(columns, key=_column_anchor)
    if not ordered:
        return cells
    name_header = next((c for c in ordered if c.field == "product_name"), None)
    name_left = name_header.x0 if name_header else 0.0
    data_cols = [c for c in ordered if c.field != "product_name" and c.x0 >= name_left - 1]
    if not data_cols:
        data_cols = [c for c in ordered if c.field != "product_name"]
    first_x = min((c.x0 for c in data_cols), default=ordered[-1].x1)
    anchors = [_column_anchor(c) for c in ordered]
    last_edge = max(c.x1 for c in ordered)
    for word in words:
        cx = (word[0] + word[2]) / 2.0
        token = word[4]
        if name_header and word[2] < name_header.x0 - 1 and re.fullmatch(r"\d{1,4}", token):
            continue
        if cx > last_edge + 80:
            continue
        if cx < first_x - 4:
            cells.setdefault("product_name", []).append(token)
            continue
        best = 0
        for index in range(len(ordered)):
            left = 0.0 if index == 0 else (anchors[index - 1] + anchors[index]) / 2.0
            right = (
                anchors[index] + 80
                if index + 1 == len(ordered)
                else (anchors[index] + anchors[index + 1]) / 2.0
            )
            if left <= cx < right:
                best = index
                break
        cells.setdefault(ordered[best].field, []).append(token)
    return cells


def _cell_number(tokens: List[str]) -> Optional[float]:
    for token in tokens:
        value = parse_number(token)
        if value is not None:
            return value
    return None


def _group_wise_ocr_items(text: str) -> Tuple[List[dict], int]:
    """Keep Group Wise rows whose trailing numbers satisfy the stock equation.

    A token that is not a plain number stops the scan. Those rows are not scored.
    """
    if not re.search(r"G?roup\s*Wise\s*Sales", text or "", re.I):
        return [], 0
    items: List[dict] = []
    skipped = 0
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        tokens = line.split()
        if len(tokens) < 4:
            continue
        nums: List[float] = []
        cut = len(tokens)
        for index in range(len(tokens) - 1, -1, -1):
            token = tokens[index]
            if token in _DASH or re.fullmatch(r"-?\d+(?:\.\d+)?", token.replace(",", "")):
                value = parse_number(token)
                if value is None:
                    break
                nums.append(value)
                cut = index
                continue
            break
        nums.reverse()
        name = re.sub(r"\s+", " ", " ".join(tokens[:cut])).strip(" |-_:")
        if not re.search(r"[A-Za-z]{3,}", name) or _junk_name(name):
            continue
        if re.search(r"himalaya drug|page\s*\d|group\s*wise", name, re.I):
            continue
        if len(nums) != 10:
            skipped += 1
            continue
        opening, purchase, sales, closing = nums[0], nums[1], nums[7], nums[9]
        middles = list(nums[2:7]) + [nums[8]]
        if any(abs(n) > 1e-9 for n in middles):
            skipped += 1
            continue
        if abs((opening + purchase - sales) - closing) > 0.51:
            skipped += 1
            continue
        items.append(
            {
                "product_code": None,
                "product_name": name,
                "packing": None,
                "opening_qty": opening,
                "opening_value": None,
                "receipts_qty": purchase,
                "receipts_value": None,
                "sales_qty": sales,
                "sales_value": None,
                "closing_qty": closing,
                "closing_value": None,
            }
        )
    return items, skipped


def _formula_for(fields: Iterable[str]) -> str:
    present = set(fields)
    adj = present & {
        "sales_return",
        "sales_return_free",
        "sales_free",
        "purchase_return",
        "purchase_free",
        "exp_damage",
        "other_receipt_qty",
        "other_issue_qty",
    }
    core = {"opening_qty", "receipts_qty", "sales_qty", "closing_qty"}
    if not core <= present:
        return "none"
    if adj:
        return "adjusted"
    return "simple"


def _detect_format(text: str) -> str:
    blob = text or ""
    checks = (
        (r"G?roup\s*Wise\s*Sales", "Group Wise Sales"),
        (r"Product\s+Stock\s+Report", "Product Stock Report"),
        (r"Stock\s+Statement\s*\(Date\s*wise\)", "Stock Statement (Datewise)"),
        (r"Saleable\s+Stock\s+Report", "Saleable Stock Report"),
        (r"ORDER\s+FORM", "Order Form"),
        (r"STOCK\s*&\s*SALES\s+ANALYSIS", "STOCK & SALES ANALYSIS"),
        (r"Sales\s*&\s*Stock\s+Statement", "Sales & Stock Statement"),
        (r"Product\s+wise\s+stock\s+statement", "Product wise stock statement"),
        (r"Stock\s*&\s*Sales\s+Statement", "Stock & Sales Statement"),
        (r"Opening\s+Balance", "Opening Balance"),
    )
    for pattern, name in checks:
        if re.search(pattern, blob, re.I):
            return name
    return "Unknown layout"


def _items_from_page(
    rows: Sequence[dict], columns: Sequence[Column], header_i: int, sub_i: Optional[int]
) -> List[dict]:
    start = (sub_i if sub_i is not None else header_i) + 1
    present = {c.field for c in columns if c.field not in {"_skip", "product_name"}}
    items: List[dict] = []
    pending = ""
    for row in rows[start:]:
        if _row_score(row["words"]) >= 6:
            pending = ""
            continue
        cells = _assign_row(row["words"], columns)
        name_tokens = list(cells.get("product_name") or [])
        name, peeled = _peel_packing(name_tokens)
        if pending:
            name = (pending + " " + name).strip()
            pending = ""
        packing_tokens = cells.get("packing") or []
        packing = " ".join(packing_tokens).strip() or peeled
        values: Dict[str, Optional[float]] = {}
        for field in present:
            if field in {"packing", "product_code", "unit_rate"}:
                continue
            if field in cells:
                values[field] = _cell_number(cells[field])
            else:
                values[field] = None
        qty_fields = [
            f
            for f in (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
            )
            if f in present
        ]
        has_qty = any(values.get(f) is not None for f in qty_fields)
        if not has_qty:
            if name and not _junk_name(name) and not re.search(
                r"HIMALAYA|ZEAL|ZANDRA|DIVISION|COMPANY", name, re.I
            ):
                pending = name
            else:
                pending = ""
            continue
        if _junk_name(name):
            continue
        code_tokens = cells.get("product_code") or []
        item = {
            "product_code": " ".join(code_tokens).strip() or None,
            "product_name": re.sub(r"\s+", " ", name).strip(),
            "packing": packing or None,
        }
        for field in CANONICAL_FIELDS:
            if field in {"product_code", "product_name"}:
                continue
            if field in present:
                item[field] = values.get(field)
            else:
                item[field] = None
        for field, value in values.items():
            if field not in item:
                item[field] = value
        items.append(item)
    return items


def parse_pages(pages: Sequence[PageSource], filename: str = "") -> dict:
    texts = [p.text for p in pages]
    full_text = "\n".join(texts)
    detected = _detect_format(full_text)
    items: List[dict] = []
    columns_found: List[str] = []
    carried: Optional[List[Column]] = None
    sources = {p.source for p in pages}
    notes: List[str] = []
    for page in pages:
        notes.extend(page.notes)
        rows = _cluster_rows(page.words)
        header_i, sub_i = _find_header(rows)
        columns: Optional[List[Column]] = None
        if header_i is not None:
            parent = rows[header_i]["words"]
            sub = rows[sub_i]["words"] if sub_i is not None else None
            columns = build_columns(parent, sub)
            if any(c.field in {"opening_qty", "closing_qty", "sales_qty"} for c in columns):
                carried = columns
            else:
                columns = None
        if columns is None and carried is not None:
            columns = carried
            header_i, sub_i = -1, None
            rows = [{"words": [], "cy": -1.0}] + rows
        if not columns:
            continue
        columns_found = [c.field for c in columns if not c.field.startswith("_")]
        page_items = _items_from_page(rows, columns, max(header_i, 0), sub_i)
        items.extend(page_items)

    present = []
    for field in list(CANONICAL_FIELDS) + [
        "sales_return",
        "sales_return_free",
        "sales_free",
        "purchase_return",
        "purchase_free",
        "exp_damage",
        "other_receipt_qty",
        "other_issue_qty",
        "total_stock",
    ]:
        if any(field == name for name in columns_found):
            present.append(field)
    source = "ocr" if sources == {"ocr"} or (sources & {"ocr"} and not items) else (
        "ocr" if "ocr" in sources and "embedded" not in sources else "embedded"
    )
    if any(p.source == "ocr" for p in pages) and not any(
        p.source == "embedded" and len(p.words) >= 12 for p in pages
    ):
        source = "ocr"
    confidence = "low"
    if items and any(f in present for f in ("opening_qty", "closing_qty", "sales_qty")):
        confidence = "medium" if source == "ocr" else "high"
        if len(items) < 2:
            confidence = "low"
    elif not items:
        notes.append("No stock-grid header with product rows was found in this PDF.")
    partial = False
    if detected == "Group Wise Sales" and source == "ocr":
        line_items, skipped = _group_wise_ocr_items(full_text)
        geometry_blank = sum(
            1 for item in items if item.get("opening_qty") is None
        ) > max(len(items) / 2, 0)
        if line_items and (geometry_blank or len(line_items) >= len(items)):
            items = line_items
            present = [
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
            ]
            columns_found = list(present)
            partial = skipped > 0
            confidence = "medium" if len(items) >= 3 else "low"
            notes = [
                note
                for note in notes
                if not str(note).startswith("No stock-grid header")
            ]
            if skipped:
                notes.append(
                    f"{skipped} scanned rows were unreadable and were not scored."
                )
    formula = _formula_for(present)
    absent = [f for f in CANONICAL_FIELDS if f not in present and f != "product_name"]
    return {
        "source_file": filename,
        "format": detected,
        "confidence": confidence,
        "source": source,
        "formula": formula,
        "present_fields": [f for f in present if f in CANONICAL_FIELDS or f in {
            "sales_return",
            "sales_free",
            "purchase_return",
            "purchase_free",
            "exp_damage",
            "other_receipt_qty",
            "other_issue_qty",
            "sales_return_free",
            "total_stock",
        }],
        "absent_fields": absent,
        "columns": columns_found,
        "line_items": items,
        "notes": notes,
        "partial": partial,
    }


def build_ground_truth(path: str, filename: str = "", max_pages: int = 20) -> dict:
    pages, notes = load_pdf_pages(path, max_pages=max_pages)
    result = parse_pages(pages, filename or path)
    result["notes"] = notes + list(result.get("notes") or [])
    return result
