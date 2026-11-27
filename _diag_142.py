from pathlib import Path

t = Path("_142_extract.log").read_text(encoding="utf-16", errors="replace")
keys = [
    "ssa_qty_value",
    "ssa_opening",
    "TOTAL STOCK",
    "vision_table",
    "SALES",
    "SEPTILIN",
    "LIV 52",
    "CYSTONE TAB",
    "column_map",
    "quality_fallback",
    "SECONDARY",
    "PREFER_CARRY",
    "pack_shift",
    "missing_sales",
    "VALUE",
    "layout=",
    "method=",
]
out = []
for line in t.splitlines():
    if any(k.lower() in line.lower() for k in keys):
        out.append(line[:280])
Path("_142_filtered.log").write_text("\n".join(out), encoding="utf-8")
print("lines", len(out))
for line in out[:120]:
    print(line)
