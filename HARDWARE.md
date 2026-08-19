# SVBONY SV405CC — comportamento medido

Tudo aqui foi **medido** nesta câmera (fw v2.0.0.6, SDK v1.13.4, macOS arm64),
não lido em documentação. Cada item é uma armadilha que corromperia o stack em
silêncio.

## Identificação

|                      |                                                    |
| -------------------- | -------------------------------------------------- |
| Sensor               | IMX294, 4144 × 2822, pixel 4,63 µm                 |
| ADC                  | 14 bits                                            |
| Cor                  | sim, Bayer **GRBG** (linha 0 = G R, linha 1 = B G) |
| Bins                 | 1, 2, 3, 4                                         |
| Formatos             | RAW8, RAW16, Y8, RGB24                             |
| TEC                  | sim, alvo de −40 a +30 °C                          |
| Ganho                | 0 a 570                                            |
| Exposição            | 36 µs a ~2000 s                                    |
| Offset (BLACK_LEVEL) | 0 a 80                                             |

## 1. RAW16 traz os 14 bits deslocados 2 bits à esquerda

Escala real **0..65532**, valores sempre múltiplos de 4. Medido pelo passo
mínimo entre valores distintos (= 4) com WB neutro.

Tratar como 0..16383 deixa tudo 4× mais claro; tratar 65535 como saturação
verdadeira erra o ponto de clipping. Use `Camera.full_scale`.

## 2. A SDK aplica white balance aos dados RAW — desligue

Com `WB_R = 400`, a fase R subiu de 724,8 para 2802,0 (≈3,9×). É multiplicação
direta sobre o mosaico Bayer: quebra a linearidade por canal, invalida flats e
destrói qualquer calibração de cor. **128 = ganho unitário.**

Efeito colateral: com WB ativo a grade de quantização de 4 desaparece, o que
mascara o item 1.

Bônus: foi assim que o padrão Bayer se confirmou — `WB_R` mexeu exatamente na
fase (0,1), consistente com GRBG.

## 3. Correção de pixel quente vem LIGADA de fábrica

`BAD_PIXEL_CORRECTION_ENABLE = 1`, limiar 60. Ela substitui pixels isolados
acima do limiar — e uma estrela fraca ocupando poucos pixels é exatamente isso.
Desligada em `_force_linear()`; fazemos nosso próprio mapa a partir dos darks.

## 4. Ganho é bloqueado enquanto a exposição está em auto

Com `EXPOSURE` em `auto=1`, o laço de `AUTO_TARGET_BRIGHTNESS` controla o ganho e
`SetControlValue(GAIN)` devolve `GENERAL_ERROR` (16). A câmera **persiste** esse
flag entre sessões, então o sintoma parece intermitente. `_leave_auto_mode()`
reafirma o valor corrente com `auto=0` no open.

## 4b. A SDK persiste parâmetros em disco, e isso morde

Por padrão a SDK grava os parâmetros da câmera num arquivo `U3SM*_Cfg_*.bin` no
**diretório de trabalho do processo** e os restaura na abertura seguinte.

É a causa raiz do item 4. A câmera voltava de uma sessão anterior com a exposição
em modo automático, o ganho ficava bloqueado, e o sintoma parecia aleatório
porque dependia de como a sessão anterior tinha terminado.

`Camera.open()` desliga com `SVBSetAutoSaveParam(id, 0)`. Reafirmamos tudo
explicitamente de qualquer forma, e estado oculto que sobrevive ao processo só
dificulta a depuração. Efeito colateral bem-vindo: para de sujar o diretório.

## 5. Reabrir exige re-enumerar

Depois de `SVBCloseCamera`, o `CameraID` só volta a ser aceito após chamar
`SVBGetNumOfConnectedCameras` / `SVBGetCameraInfo` de novo. Sem isso o segundo
`SVBOpenCamera` do processo devolve `INVALID_INDEX` (1).

## 6. Binning é software, no host — e é Bayer-aware

Medido: bin1 e bin2 levam **o mesmo tempo** (~524 ms), apesar de bin1 mover 4×
mais bytes. O frame inteiro atravessa o USB sempre; a SDK bina depois.

**Consequência: binar não acelera nada.** A escolha de bin é só escala/SNR.

O CFA é preservado — vetores de fase normalizados desviam 0,26–0,29% entre
bins, nível de ruído. É soma de pixels de mesma cor (médias escalam 4,0× e
9,0×), não média, não soma cega sobre o mosaico.

## 7. bin3 e bin4 grampeiam; bin2 é exato

A soma satura em 0xFFFF. Como cada valor já ocupa 14 bits deslocados:

| bin   | soma máx              | cabe em 16 bits?           |
| ----- | --------------------- | -------------------------- |
| 1     | 65532                 | sim, exato                 |
| **2** | 4 × 16383 = **65532** | **sim, exato — sem perda** |
| 3     | 9 × 16383 = 147447    | **não, grampeia**          |
| 4     | 16 × 16383 = 262128   | **não, grampeia muito**    |

Medido em 0,8 s: bin3 com 14% dos pixels em 65535, enquanto bin1 lia p99.9 =
7040 (11% da faixa). **bin3 perde ~3,2 stops de altas luzes.**

→ **Use bin2.** É o único bin >1 matematicamente sem perda.

## 8. USB negociado em 2.0 — troque o cabo

`PortType = USB2.0`, throughput medido 44,6 MB/s (teto prático do USB 2.0).
Resulta em **~475 ms de tempo morto fixo por frame**, em qualquer bin.

Com subs de 5–10 s isso é 5–9% de perda, tolerável. Em USB3 os 23,4 MB
atravessariam em ~70 ms. Vale trocar o cabo antes de qualquer otimização de
software.

## 9. Trocar bin/ROI precisa de folga

O primeiro frame após `SetROIFormat` pode demorar bem mais que exposição +
overhead (deu `TIMEOUT` com 4 s). Regra: descarte o primeiro frame e use
timeout generoso após qualquer mudança de geometria.

## Ainda por medir (precisa de escuro e sensor tampado)

- **Salto de HCG.** O IMX294 tem queda brusca de ruído de leitura em algum ponto
  do range 0..570. Na escala da ZWO para o mesmo sensor fica em ~120 — hipótese
  a confirmar com pares de darks em ganhos vizinhos. Define o preset de EAA.
- Amp glow em função de exposição e temperatura.
- Offset mínimo que evita truncar a cauda esquerda do ruído (0 está clipando).
