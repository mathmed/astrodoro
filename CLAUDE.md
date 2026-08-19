# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Octans é captura + live stacking para EAA no macOS (arm64), câmera SVBONY SV405CC,
voltado a um dobsoniano em plataforma equatorial — sem goto e sem autoguide. Todo o
código, comentários e mensagens estão em **português**; mantenha assim.

## Comandos

`make` sem alvo lista tudo. Os principais:

```bash
make setup                  # uv sync + make sdk + make catalog
make sdk                    # copia/reassina a dylib arm64 da SVBony em vendor/lib
make indexes                # índices do plate solver (~123 MB)
make gui                    # .venv/bin/python gui.py
make probe / usb / bench    # diagnóstico da câmera, do USB e throughput
make test                   # roda os dois testes
make lint                   # só verifica que todos os módulos importam
make solve                  # confere se astrometry.net/ASTAP e índices estão presentes
```

Variáveis dos alvos de captura: `EXP GAIN BIN FRAMES TEMP DARK OUT PASTA`
(ex.: `make dark EXP=5 GAIN=250 TEMP=-10`, `make replay PASTA=sessions/2026-08-18/2130_M8`).

**Testes não usam pytest** — são scripts com `assert` e um `if __name__ == "__main__"`.
Para rodar um só:

```bash
.venv/bin/python tests/test_warp_direction.py     # sentido da transformada
.venv/bin/python tests/test_stack_synthetic.py    # ponta a ponta, sem câmera
```

Não há linter nem formatador configurado; `make lint` é um teste de importação.

O intérprete é sempre `.venv/bin/python` (o Makefile usa `PY`), nunca o Python do
sistema — a dylib da SDK e as extensões nativas estão amarradas a esse venv.

## Trabalhar sem câmera

`ReplaySource` (`octans/source.py`) reproduz os subs FITS de uma sessão gravada
respeitando os metadados de cada frame, então o pipeline se comporta como na noite da
captura. É o caminho normal de desenvolvimento: `make replay PASTA=...` na CLI, ou
fonte "replay" na GUI. Prefira replay a mocks.

## Arquitetura

Duas frentes consomem o mesmo núcleo `octans/`:

- **GUI**: `gui.py` → `ui/main.py` (janela) + `ui/worker.py` (`CaptureWorker`, thread
  de captura/processamento que emite sinais Qt).
- **CLI**: `stack.py` (`dark`, `flat`, `run`, `replay`, `sensor`) e `probe.py`
  (diagnóstico/benchmark).

`svbony/sdk.py` é binding ctypes 1:1; `svbony/camera.py` é a camada pythônica que
neutraliza os quirks da câmera dentro de `open()`. Nada acima de `svbony/` deve falar
com a SDK direto.

### Pipeline por frame

`FrameSource` (protocolo em `octans/source.py`, implementado por `CameraSource` e
`ReplaySource`) → calibração → debayer/luminância → `stars.detect` → `register.estimate`
→ `LiveStacker.add` → `stretch` para exibição → `Recorder`.

A calibração está duplicada em `ui/worker.py::_process` e em `stack.py::cmd_run`. A
ordem é obrigatória: `(bruto − dark) / flat` → correção de pixel quente →
`/ meta.full_scale` → clip [0,1] → debayer. Mexer na calibração exige mexer nos dois
lugares.

### Invariantes que atravessam arquivos

- **Luminância é meia resolução.** `debayer.cfa_to_luminance` soma a quadra Bayer 2×2;
  as coordenadas de estrela voltam à resolução plena via `scale`/`lum_scale=2.0`, e o
  limite de saturação da detecção se compara a `debayer.LUM_SUM` (4.0), não a 1.0.
  Passar 1.0 descarta como saturada toda estrela acima de ~24% da escala do sensor.
- **A referência de registro é o stack acumulado**, não o primeiro frame. A *geometria*
  do referencial é travada no primeiro frame aceito; só a lista de estrelas é
  re-extraída (`LiveStacker._refresh_reference`). É isso que permite retomar o stack
  depois de resetar a plataforma (`new_segment()` não zera o acumulador).
- **`register.warp` recebe M no sentido direto** (src → destino), sem
  `WARP_INVERSE_MAP`. Inverter isso é o bug clássico da etapa; há autoteste dedicado.
- **Transformada é similaridade, não translação** (rotação residual da plataforma), com
  duas camadas: asterismos via astroalign e, no fallback, votação de translação para
  campos com 3–14 estrelas.
- **`background` e `stretch` só afetam a exibição.** O acumulador em float32 nunca é
  alterado por eles.
- **Rejeição é categorizada.** `FrameOutcome.kind` alimenta `LiveStacker.rejections`,
  que a GUI mostra por motivo. Um novo filtro precisa de um `kind` novo, senão a
  contagem mente.
- **Detecção só nos 70% centrais** (`central=0.70`), com piso `min_central`: coma de
  borda enviesa o centróide. O stack usa o quadro inteiro.

### GUI

- Parâmetros chegam ao worker por `request(**kw)` / `flag(name)` — dicionário sob lock,
  aplicado entre frames. **Não use slots do Qt** para isso: o laço fica bloqueado dentro
  de `SVBGetVideoData` durante toda a exposição e o event loop daquela thread não roda.
- `ui/main.py` é organizada por **modo de tarefa** (`MODES`: frame/focus/stack/adjust),
  não por categoria de configuração; cada modo tem um painel (`_panel_*`) e um contexto
  (`_ctx_*`). O modo decide o processamento, não só a exibição: frame/focus só detectam
  estrelas; stack/adjust permitem integrar (o usuário inicia explicitamente).
- `ui/design.py` (o README ainda o chama de `ui/theme.py`) tem tokens, paleta e
  componentes. No modo noturno a semântica vem do **brilho, não do matiz**, e a rampa
  vermelha passa pela imagem também, via `image_lut`.

### Sessões gravadas

`Recorder` escreve `sessions/AAAA-MM-DD/HHMM_alvo/` com `subs/` em FITS RICE,
`session.json`, previews e `stack_final.*`. O header carrega `FULLSCAL` — sem ele
qualquer ferramenta assume 65535 como saturação e erra o ponto de clipping desta câmera.

## Hardware

`HARDWARE.md` documenta nove armadilhas **medidas** nesta câmera, cada uma capaz de
corromper o stack em silêncio. Leia antes de tocar em `svbony/` ou em calibração. As de
maior consequência: RAW16 vem com os 14 bits deslocados 2 à esquerda (escala 0..65532,
use `Camera.full_scale`); a SDK aplica white balance aos dados RAW e liga correção de
pixel quente — `_force_linear()` desfaz ambos; ganho fica bloqueado com exposição em
auto, e a SDK persiste esse estado em disco entre sessões; **bin3/bin4 grampeiam, use
bin2**; binar não acelera nada.

## Dependências e vendor

`vendor/` e `data/` não estão no repositório — `make sdk`, `make catalog` e
`make indexes` os recriam. A dylib arm64 vem do bundle do AstroDMx, precisa de
`install_name_tool` para o caminho do libusb e de reassinatura ad-hoc (alterar uma
dylib em arm64 invalida a assinatura). Detalhes em `scripts/setup_sdk.sh`.

`sep` (usado em `octans/stars.py`) entra transitivamente por `astroalign`, não está
declarado no `pyproject.toml`. Plate solver nenhum vem junto; `octans/platesolve.py`
detecta astrometry.net ou ASTAP e degrada em silêncio se não achar — alinhamento polar,
push-to e anotação de catálogo dependem dele.

Sobre o PySide6 completo em vez do Essentials: já foi tentado e revertido, ver o
comentário no `pyproject.toml` antes de propor de novo.

## Convenções

Comentários explicam **por que**, com o número medido quando existe ("bin1 e bin2 levam
o mesmo tempo, ~524 ms"), e registram tentativas revertidas para não serem repetidas.
Ao mudar comportamento medido, meça de novo e atualize o número no comentário, no
README e no HARDWARE.md — os três citam valores concretos.
