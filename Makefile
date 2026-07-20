# Green Chile Reviews — transcript fetcher + analyzer
#
# `make` with no target prints help. Common flow: `make install` once, then
# `make all` to run the full pipeline (fetch → analyze → addresses → analyze).
# `make retry` re-attempts IP-blocked videos after the rate-limit window has cleared.

URL    ?= https://www.youtube.com/@GreenChileReviews/shorts
PYTHON  = .venv/bin/python
PIP     = $(PYTHON) -m pip
# yt-dlp is brew-installed; make sure /opt/homebrew/bin is on PATH for it
SHELL   = /bin/bash
export PATH := /opt/homebrew/bin:$(PATH)

.PHONY: help install fetch retry retry-all analyze map addresses all serve clean clean-cache

help:
	@echo "Targets:"
	@echo "  install      Create .venv and install Python deps"
	@echo "  fetch        Fetch transcripts not already cached  (URL=... to override)"
	@echo "  retry        Re-attempt videos previously IP-blocked"
	@echo "  retry-all    Re-attempt every video that isn't ok (blocked + no-captions)"
	@echo "  analyze      Parse transcripts -> out/reviews.csv  + geocode + index.html"
	@echo "  map          Alias for analyze"
	@echo "  all          fetch + analyze + addresses + analyze (full pipeline)"
	@echo "  addresses    Read addresses from thumbnails via OCR → manual_locations.csv"
	@echo "  serve        Serve repo root on http://localhost:8765 so you can view the map"
	@echo "  clean        Remove generated artifacts (out/, index.html)"
	@echo "  clean-cache  Also wipe transcript + listing + geocode caches"

.venv:
	python3.12 -m venv .venv 2>/dev/null || /opt/homebrew/bin/python3.12 -m venv .venv

install: .venv
	$(PIP) install --quiet -r requirements.txt
	@command -v yt-dlp >/dev/null || (echo "yt-dlp missing — run: brew install yt-dlp" && exit 1)
	@echo "Setup complete."

fetch:
	$(PYTHON) transcripts.py "$(URL)"

retry:
	$(PYTHON) transcripts.py "$(URL)" --retry-blocked

retry-all:
	$(PYTHON) transcripts.py "$(URL)" --retry-all-failed

analyze: out
	$(PYTHON) analyze.py

map: analyze

out:
	@mkdir -p out

all: fetch analyze addresses
	$(PYTHON) analyze.py

addresses:
	$(PYTHON) thumbnail_addresses.py

serve:
	@echo "Open http://localhost:8765"
	@$(PYTHON) -m http.server 8765

clean:
	rm -rf out index.html

clean-cache: clean
	rm -rf transcripts
