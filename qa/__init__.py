"""Independent batch QA for sales-statement extraction.

This package does not change production handlers. It calls
``extract_sales_statement`` and compares that JSON to a separate read of the PDF.
"""
