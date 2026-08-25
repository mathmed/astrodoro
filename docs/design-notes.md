# Design notes

Decisions that were arrived at by measurement, kept here so nobody has to pay
for the same mistake twice. Each one is easy to "simplify" back into a bug, and
several were.

The code points here rather than carrying these paragraphs inline; what stays in
the source is the short reason and the number.

---

## Frame acceptance is a relative weight, not absolute limits

`core/stacker.py`

On a Dobsonian on an equatorial platform, a distorted image is the norm rather
than the exception. An absolute elongation limit measures your frames against a
telescope you do not own — it would reject a frame worth 0.67 and accept one
worth 0.22, the latter escaping the smear filter precisely by being *more*
degraded (an isotropic blur does not elongate).

So the decision is: **this frame's score divided by the rolling p90 of the last
40**, i.e. "what is it worth compared to what tonight is delivering". One
orderable axis, and always the same sentence to explain it.

`frame_score` is `flux / (noise² · FWHM² · penalty)`. For a point source the SNR
goes as `flux/(noise·FWHM)` and the optimal weight in a weighted mean is
signal/variance, which gives the first three terms for free — they are already
measured during detection. Median flux captures transparency, noise captures the
sky background, FWHM captures seeing and focus.

**Strictness moves `min_weight` and nothing else.** Values measured by sweeping
0.0 to 0.6 over two recorded sessions (68 subs of a shaky night, 184 of a calm
one):

| min_weight | shaky night (68 subs) | calm night (184 subs) |
| --- | --- | --- |
| 0.20 | 46 used, SNR 23.2 / FWHM 6.16 | 179 used, 25.6 / 7.73 |
| 0.40 | 23 used, 22.1 / 5.74 | 148 used, 25.8 / 7.58 |
| 0.60 | 16 used, 20.4 / 5.48 | 85 used, 25.7 / 7.16 |

(SNR of the faint stars / FWHM.) What the data says: stack sharpness improves
monotonically as you tighten, and faint-star SNR varies by less than 6% across
the whole range, with a shallow optimum that depends on the night (0.30 on the
shaky one, 0.50 on the calm one). So the selector is genuinely a "more signal ↔
more sharpness" control, not a "better ↔ worse" one. That is why it is exposed
as three positions and not as five separate thresholds.

### Why the reference score is a rolling p90, never the session maximum

The absolute maximum is monotonic: one exceptional frame early in the night
demotes every other frame forever. Since the weight now decides acceptance, that
would shut the tap for the rest of the session with nothing having got worse. The
rolling p90 follows the night — it rises when the seeing improves, falls when it
worsens, and no outlier locks it.

### Why elongation, halo and FWHM remain as belts

They exist only for the gross failures no score should rescue: an enormous
streak, all the flux spread out, an unrecognisable field. They are identical at
all three strictness levels, so the selector keeps one meaning.

---

## Two metrics for two different smears

`core/stars.py`

`median_elongation` sees a star stretched. `median_halo` sees the smear that
leaves the core point-like and throws the flux into a faint trail around it —
that one passed all three strictness levels with elongation 1.26.

Measured in one session (68 subs): median halo 1.46, p90 3.95, max 7.39. The
frame that motivated the filter gives 5.11 with elongation 1.26. In the same
session the frame with the *best* halo (0.97) has elongation 1.76 and is
rejected for trailing. The correlation between the two metrics is +0.20 there
and +0.03 in a calmer session: they are different defects, and no elongation
threshold covers this one. **Do not swap one for the other.**

The halo's background annulus sits at 3–4x the aperture radius deliberately.
Closer in (1.2–1.8x) it swallowed the tip of the trail itself and the measured
frame fell from 5.1 to 3.3 — the annulus measured the defect and subtracted it
from itself.

The halo also divides `frame_score`, with a deadband up to `HALO_DEADBAND`
(1.5). Without the deadband, measurement jitter in a clean field cost 4% of the
SNR gain in the synthetic test (3.11x down to 2.98x, with every frame equally
good). The penalty is linear, not squared like FWHM: the halo already states
what fraction of the flux left the core, and squaring pushed every frame above
halo 2 to the weight floor, which is a disguised discard.

---

## `best_fwhm` only moves on an accepted frame

`core/stacker.py`

It is monotonic — it only goes down — so one bad measurement contaminates the
whole session with no way back short of resetting the stack.

The update used to sit next to the test, so a frame rejected *later* (by the
reference, by registration, by the halo) still became the ruler. In one measured
case, 12 stars in an already degraded field gave FWHM 3.56 px, the frame fell at
registration, and the limit for everything after it dropped from 8.21 to 6.41 px
for the rest of the session. The worse the frame, the fewer stars and the less
stable the median, so the asymmetry ran exactly the wrong way. It now lives in
`add`'s `done`, gated on acceptance and on `>= min_ref_stars`.

---

## The registration reference is the accumulated stack

`core/stacker.py`

Not the first frame. The stack has far higher SNR than any individual sub, which
buys three things this setup needs:

- robust matching when you knock the tube and the field jumps hundreds of pixels;
- resuming the stack after resetting the equatorial platform, or on another night
  on the same target;
- graceful improvement — the more frames, the better the reference gets.

The *geometry* of the reference frame is locked by the first accepted frame; only
the star list is re-extracted. Changing the geometry would make the stack creep,
accumulating error at every refresh. This is why `new_segment()` does not clear
the accumulator.

---

## Detection uses the central 70%, with a floor

`core/stars.py`

Edge coma on a fast Newtonian biases the centroid and poisons the fit. But
giving up the edges is only worth it if the centre still yields enough stars: in
a poor field, or right after a manual recentring that left the target off centre,
requiring the centre would starve registration and the stack would stall
rejecting everything. Hence `min_central`.

The stack itself uses the whole frame.

---

## One alignment star, and the compass only before it

`pointing/orientation.py`

Gravity already locks two of the three degrees of freedom — the device knows
which way is down to a fraction of a degree. What remains is rotation about the
vertical, which is exactly what a compass gets wrong by several degrees next to a
metal tube with a mirror and a focuser. Sighting a star and naming it resolves
that degree; the same measurement also absorbs the altitude error of a crooked
mount, which is why the alignment corrects two axes rather than one.

After alignment the compass is not used again: the `alpha` that enters is always
the relative one from the gyroscope, and its unknown offset is frozen inside the
alignment matrix.

**Measured:** with the phone 2.5 degrees off axis, about 14' of error remains 10
degrees from the alignment star and about 50' at 30 degrees (the camera's field
is 55'). Hence the guidance to align close to the target.

### Why `atan2` and not `asin` for the azimuth delta

With `asin` — as in the original — the alignment only works while the azimuth
error fits in ±90 degrees. Without a compass the device's `alpha` starts with an
arbitrary offset and the initial error can be 150 degrees; `asin` returns the
supplement, the alignment "succeeds" without complaining, and the tube then
points at the wrong side of the sky. There is a test for exactly this.

### The removed two-star model

There was once a two-star model here that also measured how crooked the phone
sits relative to the tube (three parameters, least squares) and brought the error
below 1' across the whole sky. It was removed by request, for complicating the
workflow. What was learned, in case it ever comes back:

- A compass offset is a rotation about the vertical and cancels exactly with one
  star.
- What remains is **mount twist**, which is fixed in the device's own frame and
  therefore **cannot** be corrected by a rotation in the sky. Kabsch over the
  sighting vectors leaves 0.7 degrees of residual. The correct model solves for
  the sighting axis *inside the device*.

The equatorial platform needs no special treatment: the sensor measures against
gravity, so it also measures whatever the platform tilted. A Dobsonian encoder
measures the tube relative to its base and therefore lies once the platform is
running.

---

## The sky map projects in bulk

`ui/skymap.py`

Projecting the ~2200 points (674 constellation segments plus ~1500 objects) one
at a time in Python cost 13 ms per frame — 13% of a core just to redraw the map.
In matrix form (`_proj_many`) it is 5.3 ms with everything drawn.

The projection is direction-sine (`asin`), not gnomonic: at fields of 60 degrees
or more the gnomonic stretches the edges absurdly, and this map is used precisely
wide open, to work out which part of the sky you are in.

Target, alignment anchor, search hit and reticle are distinguished by **shape**,
not colour — in night mode everything is red and hue separates nothing.

---

## The frame history stores the mosaic, not the RGB

`ui/history.py`

At bin2 (2072x1411) the float32 RGB costs 35 MB per frame and the uint16 CFA
mosaic costs 5.8 MB: within the same memory budget that is 6x more frames, and
demosaicing back costs about 15 ms — once, on the click, not per frame. The
mosaic is exactly the array the worker already builds in order to demosaic, so
archiving it is storing a reference.

The budget is in **bytes**, not frames: a bin1 frame has 4x the area of a bin2
one, and a budget in frames would become 4x the memory with nobody asking.

---

## TLS on a single port

`pointing/handset.py`

The `DeviceOrientation` event only fires in a secure context. Over
`http://192.168.x.x` the browser hands out null `alpha/beta/gamma` and says
nothing — the page looks like it works and the numbers stay empty.

And the exception you grant a self-signed certificate applies to host **and**
port. With the WebSocket on a second port, `wss://` would fail the handshake
without ever asking anything — no dialog, no visible error, just a socket that
will not open. So `Handset` owns the `QSslServer`, peeks at the request header,
and hands any `Upgrade: websocket` connection straight to the
`QWebSocketServer`.

---

## Night mode: meaning from brightness, not hue

`ui/design.py`

If the whole screen is red, green-amber-red stops working as a code. So "ok",
"warning" and "problem" become dim, medium and intense red, and anything that
needs to be distinguished at a glance is distinguished by shape or stroke —
histogram channels become solid, dashed and dotted.

The red ramp goes through the image as well (`image_lut`). Painting only the
controls and leaving a white 2000x1400 nebula on screen preserves no dark
adaptation at all: the image is the program's largest light source.

---

## The mouse wheel does not change field values

`ui/main.py`

Qt's default is that scrolling over a spinbox, combo or slider changes the value.
In a scrollable panel that means changing exposure, gain or target temperature
by accident just while looking for another control — and in the dark you do not
notice. The event is swallowed at the field and resent to the nearest scroll
area's viewport, so the panel keeps scrolling; without that resend the field
would be a dead hole in the middle of the scroll.
