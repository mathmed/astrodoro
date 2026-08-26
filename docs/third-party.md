# Third-party code and data

## Licence of this project

**GPL-3.0-or-later.** Two things force it and one confirms it:

- `src/astrodoro/pointing/constellations.py` embeds Stellarium's
  `constellationship.fab` line data, licensed **GPL-2.0-or-later**.
- `src/astrodoro/pointing/orientation.py` follows the mechanics of
  **AstroHopper** (Artyom Beilis), **GPL-3.0**: the ZXY matrix of the
  DeviceOrientation event, the top of the device as the sighting axis, and a
  single alignment on a known star.

Anyone wanting to redistribute this under different terms would have to replace
both — the constellation lines with a differently licensed set, and the
orientation model with an independent derivation.

## Embedded data

| file | source | licence |
| --- | --- | --- |
| `pointing/brightstars.py` | HYG v3 (astronexus) — 925 stars to mag 4.5, J2000 | CC BY-SA 4.0 |
| `pointing/constellations.py` | Stellarium `constellationship.fab`, western asterisms — 674 segments, 88 labels | GPL-2.0-or-later |

Both are embedded in the source rather than downloaded. All of `data/` is
recreated by script and may not exist; recognising the sky and aligning are the
first things you do at night, and failing there for want of a download would be
failing before starting. Together they are about 92 KB.

## Downloaded data

| what | source | licence | how |
| --- | --- | --- | --- |
| `data/NGC.csv` | OpenNGC (Mattia Verga) — complete NGC + IC, ~14k objects | CC BY-SA 4.0 | `astrodoro catalog` |
| object thumbnails | DSS2 colour, cut out by the CDS `hips2fits` service (Strasbourg) | see below | fetched on demand into the user's own cache |

### About the thumbnails

The pictures in the TARGETS panel are cutouts of the **Digitized Sky Survey**,
rendered by the CDS `hips2fits` service from the `CDS/P/DSS2/color` HiPS. They
are **not redistributed with this program**: each installation fetches what it
looks at into its own cache directory (`previews/` under the user data folder),
one 512x512 JPEG per object and field, about 30 KB each.

The DSS plates are free for non-commercial use with acknowledgement, which is
why "DSS2" is painted on every thumbnail rather than kept in a credits screen.
The full acknowledgement the survey asks for:

> Based on photographic data obtained using the Oschin Schmidt Telescope on
> Palomar Mountain and the UK Schmidt Telescope, digitised by the Space
> Telescope Science Institute. Served by the Centre de Données astronomiques de
> Strasbourg (CDS).

Anyone repackaging this program commercially has to check those terms; nothing
else in the feature depends on the service, and `core/previews.py` is one
`SERVICE` constant away from another provider.

## Vendor binaries

The SVBony Camera SDK (`libSVBCameraSDK.dylib`, API 3.0.0 / lib v1.13.4) is
proprietary and **not redistributed here**. `make sdk` takes the arm64 build from
a local AstroDMx installation, rewrites its libusb path and re-signs it ad hoc;
`vendor/` is gitignored. For distribution, obtain the SDK from SVBony directly.

The indi-3rdparty macOS build is x86_64 only, which is useless on Apple Silicon
natively — hence the AstroDMx route for development.

## Prior art this learned from

- **AstroHopper** (Artyom Beilis) — the phone-as-setting-circle approach, and the
  one-star alignment workflow.
- **PixInsight** — STF/MTF autostretch and DBE background extraction, whose
  behaviour `core/stretch.py` and `core/background.py` reimplement at live-stack
  speed.
- **Lupton et al.** — the colour-preserving arcsinh construction used for the
  SDSS images, in `stretch.auto_arcsinh`.
- **astroalign** (Martin Beroiz) — asterism matching, used directly.
- **sep** (Kyle Barbary) — Source Extractor as a library, used directly.
