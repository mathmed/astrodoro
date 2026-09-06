# Project shortcuts. `make` on its own lists everything.
PY      := .venv/bin/python
EXP     ?= 5
GAIN    ?= 250
BIN     ?= 2
FRAMES  ?= 20
TEMP    ?=
BIAS    ?=
DARK    ?=
FLAT    ?=
OUT     ?=
FOLDER  ?=

TEMPARG := $(if $(TEMP),--target-temp $(TEMP),)
BIASARG := $(if $(BIAS),--bias $(BIAS),)
DARKARG := $(if $(DARK),--dark $(DARK),)
FLATARG := $(if $(FLAT),--flat $(FLAT),)
OUTARG  := $(if $(OUT),--out $(OUT),)

.DEFAULT_GOAL := help
.PHONY: help setup sdk catalog gui probe info usb bench tec bias dark flat \
        run replay handset settings bundle brand test lint fmt i18n clean distclean

help:  ## show this list
	@echo "Astrodoro — capture and live stacking for EAA"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-11s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "variables:  EXP=$(EXP)  GAIN=$(GAIN)  BIN=$(BIN)  FRAMES=$(FRAMES)"
	@echo "            TEMP=  BIAS=  DARK=  FLAT=  OUT=  FOLDER="
	@echo "example:    make dark EXP=5 GAIN=250 TEMP=-10"

# --------------------------------------------------------------------- setup
setup: ## create the venv, prepare the SDK and download the catalogue
	uv sync --extra dev
	$(MAKE) sdk
	$(MAKE) catalog
	@echo "ready. 'make gui' to open it."

sdk: ## copy and fix up the SVBony arm64 dylib
	scripts/setup_sdk.sh

catalog: ## download OpenNGC (the deep-sky object catalogue)
	$(PY) -m astrodoro.cli catalog

# ----------------------------------------------------------------------- use
gui: ## open the interface
	$(PY) -m astrodoro.ui

probe: ## camera diagnostics
	$(PY) -m astrodoro.cli info

info: probe

usb: ## USB path diagnostics
	$(PY) -m astrodoro.cli usb

bench: ## measure real throughput
	$(PY) -m astrodoro.cli bench --bin $(BIN)

tec: ## monitor the cooler (TEMP=-10)
	$(PY) -m astrodoro.cli tec --target $(if $(TEMP),$(TEMP),-10)

sensor: ## find the gain step and the minimum offset (cap the sensor)
	$(PY) -m astrodoro.cli sensor --bin $(BIN)

bias: ## master bias (cap the sensor; the exposure is the camera's minimum)
	$(PY) -m astrodoro.cli bias --gain $(GAIN) --bin $(BIN) \
	  --frames $(FRAMES) $(OUTARG)

dark: ## master dark (cap the sensor)
	$(PY) -m astrodoro.cli dark --exp $(EXP) --gain $(GAIN) --bin $(BIN) \
	  --frames $(FRAMES) $(TEMPARG) $(OUTARG)

flat: ## master flat (evenly illuminated surface)
	$(PY) -m astrodoro.cli flat --exp $(EXP) --gain $(GAIN) --bin $(BIN) \
	  --frames $(FRAMES) $(BIASARG) $(DARKARG) $(OUTARG)

run: ## headless live stacking session
	$(PY) -m astrodoro.cli run --exp $(EXP) --gain $(GAIN) --bin $(BIN) \
	  $(BIASARG) $(DARKARG) $(FLATARG) $(OUTARG)

replay: ## reprocess a recorded session (FOLDER=~/Astrodoro/sessions/...)
	@test -n "$(FOLDER)" || { echo "usage: make replay FOLDER=~/Astrodoro/sessions/2026-08-18/2130_M8"; exit 1; }
	$(PY) -m astrodoro.cli replay $(FOLDER) $(BIASARG) $(DARKARG) $(FLATARG) $(OUTARG)

settings: ## show the stored settings
	$(PY) -m astrodoro.cli settings

# --------------------------------------------------------------- development
bundle: ## build build/Astrodoro.app, so macOS names the program properly
	$(PY) scripts/make_app.py

brand: ## re-derive the logo and icon variants from the masters
	$(PY) scripts/derive_brand.py

handset: ## fake phone, to work on push-to without going outside
	$(PY) scripts/fake_handset.py

test: ## run the test suite
	$(PY) -m pytest

lint: ## ruff check plus a stale-catalogue check
	$(PY) -m ruff check src tests scripts
	$(PY) scripts/extract_messages.py --check

fmt: ## apply the fixes ruff can make on its own
	$(PY) -m ruff check --fix src tests scripts

i18n: ## refresh the .pot and report what pt_BR still lacks
	$(PY) scripts/extract_messages.py
	-$(PY) scripts/extract_messages.py --missing pt_BR

clean: ## remove caches
	find . -name __pycache__ -type d -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache

distclean: clean ## also remove the venv and the downloaded data
	rm -rf .venv vendor/lib data/NGC.csv
