# Octans

*A constelação do polo sul celeste — aquele que você não consegue ver.*

Captura e live stacking para EAA no macOS. Nasceu porque não existe equivalente
ao SharpCap no Mac: dá para capturar (AstroDMx, oaCapture) ou empilhar ao vivo
via pasta monitorada (Siril, ASTAP, ALS), mas não há nada que faça as duas
coisas integradas num só laço.

Estado: **funcionando, com interface gráfica.** Captura, calibra, detecta
estrelas, registra, empilha, exibe com autostretch em tempo real e salva.

Câmera suportada: SVBONY SV405CC (a arquitetura isola o driver; portar outro
fabricante é trocar `svbony/` por um módulo equivalente).

## Instalação

```bash
uv sync                 # cria .venv com numpy, opencv, astropy, sep, astroalign
scripts/setup_sdk.sh    # copia e ajusta a dylib arm64 da SVBony
```

## Uso

### Interface gráfica

```bash
.venv/bin/python gui.py
```

A interface é organizada por **modo de tarefa**, não por categoria de
configuração: cada fase da noite tem necessidades de tela diferentes, e o modo
reorganiza os dois lados.

| modo | atalho | painel | contexto ao lado da imagem |
|---|---|---|---|
| Enquadrar | `1` | fonte, exposições rápidas, plate solve, push-to, local | direção do alvo em tipo grande e objetos no campo |
| Focar | `2` | HFR grande, veredito, bipe | lupa 5× e HFR ao longo do tempo |
| Integrar | `3` | alvo, gravação, dark, sigma clip, plataforma | histograma e rotação residual |
| Ajustar | `4` | presets de stretch, sliders, tela, tema | histograma em largura cheia |

Enquadrar e Focar trocam a exibição para o frame ao vivo; Integrar e Ajustar,
para o stack.

Outros atalhos: `F` só a imagem, `N` modo noturno, `L` log, `Espaço` novo
segmento, `Ctrl+S` salvar.

**Barra de sinais vitais**, sempre visível: estado, integração, frames
aceitos·rejeitados, HFR e curso da plataforma — em tipo grande, porque no escuro
você olha de relance e não lê painéis. Abaixo, a barra de exposição, que evita o
programa parecer travado durante um sub de 10 s, e a **tira de saúde dos
frames**: os últimos noventa como marcas de aceito/rejeitado. A sequência importa
mais que a contagem — três rejeições isoladas em cinquenta frames é seeing; doze
seguidas é nuvem, orvalho ou alvo fora do quadro.

**Alertas** aparecem em faixa acima da imagem quando algo precisa de ação:
rejeições em sequência, plataforma perto do fim do curso, integração passando do
orçamento de rotação.

### Modo noturno

Três níveis de brilho, e a imagem inteira passa por uma rampa vermelha — não só
os controles. A imagem é a maior fonte de luz do programa; pintar os botões de
vermelho e deixar uma nebulosa branca de 2000×1400 na tela não preserva
adaptação nenhuma.

No noturno **a semântica vem do brilho, não do matiz**: se toda a tela é
vermelha, verde-amarelo-vermelho deixa de funcionar como código, então ok,
atenção e problema viram vermelho apagado, médio e intenso.

Há também um modo de alvos de clique grandes, para uso no escuro.

### Linha de comando

```bash
# diagnóstico da câmera
.venv/bin/python probe.py info

# throughput real (revela se o USB está limitando)
.venv/bin/python probe.py bench --bin 2

# master dark + mapa de pixels quentes (tampe o sensor)
.venv/bin/python stack.py dark --exp 5 --gain 250 --frames 20 --target-temp -10

# master flat (superfície uniformemente iluminada)
.venv/bin/python stack.py flat --gain 250 --exp 0.05 --frames 16 \
    --dark darks/dark_g250_e0.1s_-10C.fits

# sessão de live stacking
.venv/bin/python stack.py run --exp 5 --gain 250 \
    --dark darks/dark_g250_e5.0s_-10C.fits --out session/
```

Durante a sessão: `Ctrl-C` encerra e salva; **Enter marca um novo segmento** —
use depois de resetar a plataforma equatorial ou recentrar o tubo.

Saídas em `session/`: `live.png` (frame corrente, para enquadrar e focar),
`stack.png` (stack com autostretch, atualizado a cada frame),
`stack_final.png` + `stack_final.fits` no encerramento.

## Arquitetura

```
svbony/sdk.py         binding ctypes 1:1 do SVBCameraSDK
svbony/camera.py      camada pythônica; neutraliza os quirks da SDK no open()
octans/source.py   fontes de frame: câmera ao vivo e replay de pasta
octans/recorder.py gravação de subs (FITS RICE), stacks e metadados
octans/debayer.py  CFA -> RGB (código do OpenCV determinado empiricamente)
octans/stars.py    detecção via sep, FWHM, filtros de qualidade
octans/register.py casamento de asterismos (astroalign) -> similaridade
octans/stacker.py  acumulador float32 + mapa de peso + sigma clip corrente
octans/stretch.py  autostretch MTF/STF, e LUT 16->8 bits para exibição
octans/background.py  extração de gradiente por envoltória inferior
octans/focus.py    HFR, tendência, lupa
octans/platform.py curso da plataforma, rotação residual, orçamento de arrasto
octans/polar.py    alinhamento polar por ajuste geométrico do eixo
octans/platesolve.py  envelope sobre astrometry.net / ASTAP
octans/catalog.py  OpenNGC: "o que estou fotografando"
octans/pushto.py   círculo graduado digital, sem encoder
ui/worker.py          thread de captura+processamento, sinais para a GUI
ui/main.py            janela: quatro abas + imagem
ui/theme.py           tema escuro e modo noturno (inclusive a imagem)
ui/audio.py           bipes de foco
gui.py                ponto de entrada da interface
stack.py              CLI: thread de captura -> fila -> empilhamento
probe.py              diagnóstico, benchmark de USB e sensor
```

Três decisões que diferem do óbvio, todas ditadas pelo alvo (dobsoniano com
plataforma equatorial, sem goto, sem autoguide):

**A referência de registro é o próprio stack acumulado, não o primeiro frame.**
O stack tem SNR muito maior que qualquer sub, o que dá casamento robusto quando
você encosta no tubo, permite retomar depois do reset da plataforma, e melhora
sozinho conforme a integração cresce. A geometria do referencial fica travada no
primeiro frame aceito; só a lista de estrelas é reextraída.

**Transformada de similaridade, não translação.** Plataforma equatorial alinhada
no olho deixa rotação residual; ignorá-la borra o stack em poucos minutos.

**Detecção só nos 70% centrais.** Dobsoniano rápido tem coma nas bordas, e
estrela comática enviesa o centróide. O stack usa o quadro inteiro; o registro
confia apenas no centro. Com piso: se o centro render poucas estrelas, abre para
o quadro todo.

## Algoritmos

Tudo abaixo foi medido em cenários sintéticos com verdade conhecida. Os números
são do repositório, não de literatura.

### Registro em duas camadas

**Asterismos** (astroalign) como primeira camada: casa triângulos por invariantes
de razão de lados, robusto a translação, rotação e escala arbitrárias. É o que
sobrevive ao salto de centenas de pixels quando você empurra o tubo.

**Votação de translação** quando o primeiro falha. O astroalign precisa de
algumas dezenas de estrelas para convergir; com 8 a 14 ele estoura em
`MaxIterError`. E é exatamente esse o frame que nuvem fina, luar ou campo pobre
produzem — muitas vezes **com FWHM ótimo**. A votação percorre as translações
implicadas por pares (estrela do frame, estrela da referência), toma a de mais
votos e ajusta uma similaridade sobre os pares casados, recuperando também a
rotação. Funciona com 3 ou 4 estrelas.

Medido numa noite sintética variável de 24 frames (8 ótimos, 4 com seeing ruim,
3 com nuvem fina, 3 com vibração, 6 bons):

| configuração | frames usados | SNR | equivalente em tempo |
|---|---|---|---|
| só asterismos, sem peso | 14/24 | referência | — |
| só asterismos, com peso | 14/24 | −0,4% | −1% |
| + votação, sem peso | 21/24 | +1,7% | +4% |
| **+ votação, com peso** | **21/24** | **+4,2%** | **+8%** |

Os sete frames recuperados tinham FWHM entre 1,8 e 3,5 — utilizáveis. E há um
ganho que o SNR não mede: sem a votação, um trecho da sessão com poucas estrelas
te dá **zero**; com ela, você continua integrando.

### Ponderação por qualidade

Peso ∝ fluxo/(ruído²·FWHM²) — para fonte pontual o SNR vai como
fluxo/(ruído·FWHM), e o peso ótimo de uma média ponderada é sinal/variância. Os
três termos já vêm da detecção de estrelas, então é de graça.

**Sozinha ela não serve para nada** (−0,4% na tabela acima): os frames que
receberiam peso baixo são os mesmos que falhavam no registro. Ela só rende
somada à votação, que deixa esses frames entrarem — o peso é o que impede que
puxem o stack para baixo.

### Extração de gradiente de fundo

Superfície polinomial ajustada à **envoltória inferior** das amostras, não por
mínimos quadrados simples. A diferença importa: rejeição só de um lado, porque o
que fica acima do modelo é sinal (nebulosa, galáxia, halo) e nunca céu. Amostra
por blocos com percentil baixo, porque uma nebulosa de 200 px cobre blocos
inteiros e ali o sinal não é outlier, é deslocamento de nível.

Medido contra um gradiente injetado (rampa + amp glow gaussiano) com uma nebulosa
extensa no campo:

| grau | erro do ajuste | nebulosa absorvida | gradiente removido |
|---|---|---|---|
| 1 | 8,4% | 1,9% | 57% |
| **2** | **5,9%** | **5,4%** | **71%** |
| 3 | 13,2% | 14,7% | — |

Grau 3 degrada, e é por isso que o controle para em 2. Antes da correção para
envoltória inferior, o grau 2 chegava a **piorar** o resultado (19% de erro, 26%
da nebulosa absorvida).

Só afeta a exibição — o acumulador não é alterado.

### Descarte de frames ruins

Quatro níveis, onze mecanismos:

**Por frame** — contagem mínima de estrelas; FWHM contra o melhor da sessão;
**elongação mediana** (frame arrastado); **salto de fundo** contra a mediana
corrente; mínimo de estrelas para iniciar o referencial.

**Por registro** — falha do casamento, pares casados insuficientes, inliers
insuficientes, rms acima do limite.

**Por estrela, na detecção** — recorte central (coma de borda), saturadas,
elongação individual acima de 3 (trilha de satélite, raio cósmico).

**Por pixel** — sigma clip corrente (satélite, avião) e máscara de cobertura.

Três desses são específicos de dobsoniano e não existem em software genérico:

**Elongação mediana.** O filtro por estrela pega uma trilha isolada. Mas se você
encostou no tubo *durante* a exposição, todas as estrelas ficam alongadas na mesma
direção e todas passam individualmente — só a mediana revela.

**Salto de fundo.** Farol de carro, lua nascendo, orvalho no espelho. FWHM e
contagem podem passar, e o frame só acrescenta ruído.

**Janela de assentamento.** Depois de um salto grande o dobsoniano vibra. Em vez
de descartar às cegas os próximos frames, exige FWHM mais apertado por dois
frames — assim um frame já bom não é jogado fora.

Os limiares são ajustados em grupo por um seletor de três posições, e o painel
**frames descartados** mostra a contagem por motivo, que é o que diz se vale
mexer no rigor, refocar ou esperar a nuvem passar.

| rigor | frames usados (cenário de 14 com 4 defeitos) |
|---|---|
| tolerante | 11 |
| normal | 9 |
| rigoroso | 8 |

### Stretch: MTF e arcsinh

**MTF/STF** é o padrão, mesma família do PixInsight e do SharpCap: ponto preto na
mediana menos 2,8 desvios robustos, meio-tom resolvido em forma fechada para
levar o fundo ao alvo. Aplicado por LUT de 65536 entradas, o que deixa o slider
em 5,6 ms num frame de bin2 contra 75 ms do caminho aritmético.

**arcsinh** como alternativa para alvo com núcleo brilhante. A curva é aplicada à
luminância e os canais entram pela razão que têm com ela, preservando as
proporções de cor (construção de Lupton et al., das imagens do SDSS).

Medido com uma nebulosa realmente vermelha (R/B = 1,82) e canais já igualados:

| | fundo R/B | nebulosa R/B | núcleo da estrela |
|---|---|---|---|
| MTF por canal | 1,01 | 1,10 | 0,996 |
| arcsinh | 1,18 | **1,71** | 1,000 |

O MTF por canal achata a cor real para 1,10; o arcsinh guarda 1,71 de 1,82.

**Ressalva que vale saber**: o arcsinh preserva as razões de cor, o que inclui o
desvio de resposta do sensor. Sem flat o céu sai colorido (fundo em R/B 1,58). O
MTF por canal é imune, porque normaliza cada canal em separado — por isso ele
segue o padrão. **arcsinh quer flat.**

## Comportamento da câmera

Ver [HARDWARE.md](HARDWARE.md) — nove armadilhas medidas nesta câmera, cada uma
capaz de corromper o stack em silêncio. Resumo do que mais importa:

- RAW16 traz os 14 bits deslocados 2 bits à esquerda (escala 0..65532)
- a SDK aplica **white balance aos dados RAW**; `_force_linear()` neutraliza
- correção de pixel quente vem ligada e come estrelas fracas; desligada
- ganho é bloqueado enquanto a exposição está em auto
- binning é software e Bayer-aware; **bin3/bin4 grampeiam, use bin2**
- binar não acelera nada — o frame inteiro sempre atravessa o USB

## Testes

```bash
.venv/bin/python tests/test_warp_direction.py    # sentido da transformada
.venv/bin/python tests/test_stack_synthetic.py   # ponta a ponta, sem câmera
```

O teste sintético simula deriva, rotação residual, um satélite e um salto de
campo de 180 px no meio da sequência (reset da plataforma). Verifica registro
sub-pixel, ganho de SNR e remoção do satélite.

## Módulos específicos deste setup

Um dobsoniano em plataforma equatorial, sem goto e sem autoguide, tem problemas
que nenhum software de EAA trata. Estes três são a razão de existir do projeto.

### Gravação e replay

`stack.py replay pasta/` alimenta o pipeline exatamente como a câmera faria,
respeitando os metadados de cada sub. Uma noite de captura vira quantas
iterações de desenvolvimento você quiser — foco, alinhamento, limiares, tudo
testável de dia. A GUI também aceita replay como fonte.

Subs em FITS com compressão RICE (sem perda) e cabeçalho completo, incluindo
`FULLSCAL` — sem isso qualquer ferramenta assume 65535 como saturação e erra o
ponto de clipping desta câmera.

### Plataforma equatorial

Cronômetro de curso, e o número que realmente importa: **integração útil**.
A rotação residual medida frame a frame no registro dá a taxa de rotação de
campo; multiplicada pelo raio do canto, dá o arrasto em px/min; dividindo o FWHM
por isso, sai quanto tempo você pode integrar antes das estrelas da borda
virarem risco. Com 0,02°/min de rotação residual são cerca de 7 minutos — muito
antes de a plataforma acabar o curso.

`Resetei a plataforma` reinicia o cronômetro sem zerar o stack: o próximo frame é
registrado contra o que já foi integrado.

### Alinhamento polar no céu do sul

No hemisfério sul não há Polar — Sigma Octantis é magnitude 5,4 e não se acha em
céu urbano. `polar.py` resolve isso por geometria: pontos que giram em torno de um
eixo descrevem um círculo, e esse círculo está num plano perpendicular ao eixo.
Resolvendo o campo em três ou mais momentos e ajustando um plano aos vetores, a
normal **é** o eixo real da plataforma. Comparando com o polo celeste sai a
correção em altitude e azimute, em minutos de arco.

Exato, sem fórmulas aproximadas de drift align, e serve com qualquer alvo em
qualquer parte do céu — não exige estrela no meridiano nem no horizonte leste.
Validado em malha fechada: desalinhamentos injetados de 0,05° a 3° são
recuperados com erro nulo.

Precisa de plate solve. Sem solver instalado, tudo degrada em silêncio.

## Plate solving

Nenhum solver vem junto. Duas opções:

```bash
brew install astrometry-net      # solver
scripts/get_indexes.sh           # índices para este campo, ~200 MB
```

ou ASTAP, baixado de hnsky.org (não está no Homebrew) mais um banco de estrelas.

O envelope detecta o que estiver presente. Com solução, você ganha: anotação dos
objetos no campo (OpenNGC, 12 mil objetos), push-to em altitude/azimute, e o
alinhamento polar.

## Próximos passos

1. **Medir o salto de HCG do IMX294** (pares de darks em ganhos vizinhos, sensor
   tampado). Define o preset de ganho para EAA. Hipótese: ~120 na escala 0..570.
2. **Assistente de exposição**: do fundo de céu medido, recomendar o tempo de sub
   que deixa o fundo em 10-20% do poço (regime limitado pelo céu).
3. Fluxo guiado de alinhamento polar na GUI (resolver, girar, resolver, corrigir).
4. Nuvem vs. orvalho: queda de contagem com FWHM estável é nuvem; FWHM subindo
   devagar é orvalho. Reportar em vez de só rejeitar em silêncio.
5. Deriva de foco por temperatura: registrar HFR contra temperatura e avisar.
6. Testar se ROI pequena acelera o laço de foco (binning não acelera — o frame
   inteiro sempre atravessa o USB; recorte pode ser diferente).
7. Portar um segundo fabricante de câmera (a API dos SDKs é quase isomórfica).
8. Sigma clip com janela real. O atual compara com a média corrente, contaminada
   no início. Guardar os últimos ~20 frames alinhados custa ~120 MB em bin2 e
   permite mediana e MAD verdadeiros por pixel.
9. Interpolação cúbica ou Lanczos no warp em vez de bilinear. A 1,59"/px com
   seeing de 2–3" a suavização custa resolução real.
10. Medir a ponderação e a votação nos **seus** dados via replay, em vez de em
    cenário sintético.

## Créditos

Catálogo: [OpenNGC](https://github.com/mattiaverga/OpenNGC) de Mattia Verga,
CC-BY-SA 4.0.
