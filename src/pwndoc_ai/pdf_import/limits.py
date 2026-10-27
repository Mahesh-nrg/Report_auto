"""Limits of the PDF importer (docs/EXPORT_DESIGN.md (g)). Every number that bounds work lives
here so the precheck, the worker and the tests agree on them."""

from __future__ import annotations

MIB = 1024 * 1024

# upload precheck (in process and again in the worker)
MAX_PDF_BYTES = 100 * MIB
MAX_PAGES = 400  # len(reader.pages), never /Count
MAX_PAGE_SIDE = 14400.0  # points (200 inches)
MAX_PAGE_STREAM = 16 * MIB  # decoded content of one page, forms included
MAX_TOTAL_STREAM = 128 * MIB  # decoded content of the whole file
MAX_IMAGE_PIXELS = 80_000_000
MAX_TOTAL_PIXELS = 400_000_000
MAX_ACTION_DEPTH = 8
MAX_FORM_DEPTH = 3
MAX_ANNOTS_PER_PAGE = 4000
MAX_XOBJECTS_PER_RESOURCE = 512
MAX_STREAM_PARTS = 256
PRECHECK_SECONDS = 15.0
HEAD_BYTES = 1024

# text layer
TEXT_LAYER_PAGES = 50
MIN_TEXT_CHARS = 25
IMAGE_COVER = 0.5

# worker subprocess
WORKER_TIMEOUT = 180.0
WORKER_CPU_SECONDS = 120
STRUCTURE_MEMORY_MB = 512
PDF2DOCX_MEMORY_MB = 3072
MAX_REPORT_BYTES = 1 * MIB
REAP_SECONDS = 5.0

# external tools inside the worker
TOOL_TIMEOUT = 60.0
CHUNK_PAGES = 10  # mutool is run on page ranges so each JSON stays small
MAX_TOOL_OUTPUT = 96 * MIB
MAX_TRACE_POINTS = 64  # a path with more points is not a ruling line or a cell
MAX_RULES_PER_PAGE = 3000
MAX_SEGMENTS_TABLES = 600  # union-find over segments is quadratic

# images
MAX_IMAGES = 60
MAX_IMAGES_PER_PAGE = 12
CROP_WORKERS = 4
CROP_DPI = 110
MIN_IMAGE_POINTS = 20.0

# output
MAX_OUTPUT_BYTES = 200 * MIB
