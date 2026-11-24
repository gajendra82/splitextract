"""Multi-vendor HTML table extract: template → fuzzy → LLM column mapping."""

from services.html_table.html_table_parser import parse_html_table
from services.html_table.column_matcher import COLUMN_PATTERNS, match_headers
from services.html_table.templates import TEMPLATES, route_template

__all__ = [
    "COLUMN_PATTERNS",
    "TEMPLATES",
    "match_headers",
    "parse_html_table",
    "route_template",
]
