# Atalhos do projeto. `make` sozinho lista tudo.
PY      := .venv/bin/python
EXP     ?= 5
GAIN    ?= 250
BIN     ?= 2
FRAMES  ?= 20
TEMP    ?=
DARK    ?=
OUT     ?=
PASTA   ?=

TEMPARG := $(if $(TEMP),--target-temp $(TEMP),)
DARKARG := $(if $(DARK),--dark $(DARK),)
OUTARG  := $(if $(OUT),--out $(OUT),)

.DEFAULT_GOAL := help
.PHONY: help setup sdk catalog indexes gui probe info usb bench tec dark flat \
        run replay solve test lint clean distclean

help:  ## mostra esta lista
	@echo "octans — captura e live stacking para EAA"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-11s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "variáveis:  EXP=$(EXP)  GAIN=$(GAIN)  BIN=$(BIN)  FRAMES=$(FRAMES)"
	@echo "            TEMP=  DARK=  OUT=  PASTA="
	@echo "exemplo:    make dark EXP=5 GAIN=250 TEMP=-10"

# ---------------------------------------------------------------- preparação
setup: ## cria o venv, prepara a SDK e baixa o catálogo
	uv sync
	$(MAKE) sdk
	$(MAKE) catalog
	@echo "pronto. 'make gui' para abrir."

sdk: ## copia e ajusta a dylib arm64 da SVBony
	scripts/setup_sdk.sh

catalog: ## baixa o OpenNGC (catálogo de objetos)
	scripts/get_catalog.sh

indexes: ## baixa os índices do plate solver (~123 MB)
	scripts/get_indexes.sh minimo

# ---------------------------------------------------------------- uso
gui: ## abre a interface
	$(PY) gui.py

probe: ## diagnóstico da câmera
	$(PY) probe.py info

info: probe

usb: ## diagnóstico do caminho USB
	$(PY) probe.py usb

bench: ## mede throughput real
	$(PY) probe.py bench --bin $(BIN)

tec: ## monitora a refrigeração (TEMP=-10)
	$(PY) probe.py tec --target $(if $(TEMP),$(TEMP),-10)

sensor: ## acha o salto de ganho e o offset mínimo (tampe o sensor)
	$(PY) stack.py sensor --bin $(BIN)

dark: ## master dark (tampe o sensor)
	$(PY) stack.py dark --exp $(EXP) --gain $(GAIN) --bin $(BIN) \
	  --frames $(FRAMES) $(TEMPARG) $(OUTARG)

flat: ## master flat (superfície uniformemente iluminada)
	$(PY) stack.py flat --exp $(EXP) --gain $(GAIN) --bin $(BIN) \
	  --frames $(FRAMES) $(DARKARG) $(OUTARG)

run: ## sessão de live stacking sem interface
	$(PY) stack.py run --exp $(EXP) --gain $(GAIN) --bin $(BIN) \
	  $(DARKARG) $(OUTARG)

replay: ## reprocessa uma sessão gravada (PASTA=sessions/...)
	@test -n "$(PASTA)" || { echo "uso: make replay PASTA=sessions/2026-08-18/2130_M8"; exit 1; }
	$(PY) stack.py replay $(PASTA) $(DARKARG) $(OUTARG)

# ---------------------------------------------------------------- manutenção
solve: ## verifica se o plate solver está instalado e configurado
	@$(PY) -c "import sys; sys.path.insert(0,'.'); \
from octans.platesolve import solvers_available, install_hint, LOCAL_CFG; \
a=solvers_available(); \
[print(f'  {k:<16} {v or chr(45)}') for k,v in a.items()]; \
print(f'  cfg local        {LOCAL_CFG if LOCAL_CFG.exists() else chr(45)}'); \
print() if any(a.values()) else print(install_hint())"
	@ls -la data/indexes/*.fits 2>/dev/null || echo "  sem índices — rode 'make indexes'"

test: ## roda os testes
	$(PY) tests/test_warp_direction.py
	$(PY) tests/test_stack_synthetic.py

lint: ## verifica que todos os módulos importam
	@$(PY) -c "import sys; sys.path.insert(0,'.'); import importlib; \
mods='svbony.sdk svbony.camera octans.source octans.recorder \
octans.debayer octans.stars octans.register octans.stacker \
octans.stretch octans.background octans.focus octans.platform \
octans.polar octans.platesolve octans.catalog octans.pushto \
octans.cooling ui.design ui.icons ui.audio ui.worker ui.main'.split(); \
[importlib.import_module(m) for m in mods]; \
print(f'{len(mods)} módulos importam sem erro')"

clean: ## remove caches e saídas temporárias
	find . -name __pycache__ -type d -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf session replay .pytest_cache

distclean: clean ## remove também o venv e os dados baixados
	rm -rf .venv data/indexes vendor/lib
