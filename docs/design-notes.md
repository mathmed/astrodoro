# Design notes

Decisions that were arrived at by measurement, kept here so nobody has to pay
for the same mistake twice. Each one is easy to "simplify" back into a bug, and
several were.

The code points here rather than carrying these paragraphs inline; what stays in
the source is the short reason and the number.

---

## Bias and dark are alternatives, and the flat wants the bias

`core/calibration.py`, `core/masters.py`

The program shipped with darks and flats and no bias, which is defensible for
EAA — a dark of the right exposure removes the pedestal along with the thermal
signal, so the bias has nothing left to do on a light frame. Two things brought
it in.

**The flat.** A flat is milliseconds long; the session's dark is seconds. The
flat capture subtracted whatever dark was loaded, and mid-session that dark is
of the *lights'* exposure — so it carried thermal signal the flat never
collected, and the subtraction dug a hole in the middle of the correction.
Without any subtraction the failure is the other way round: the offset pedestal
turns into a multiplicative error. Measured on a 1920x1080 synthetic flat with
50% corners, the 500 ADU pedestal this camera runs at offset 20, and a flat
median of 20000 ADU: the corner reads 0.6064 instead of 0.5946 — the curve
comes out 2.0% shallow — and the calibrated light keeps **2.4% of the falloff**
it was supposed to have removed. Small, and always in the same direction, which
is what makes it a gradient in the stack rather than noise; with the bias
subtracted the same measurement leaves 0.00%. A bias is the pedestal at *any*
exposure, so it is the master that belongs there.

**The dark you do not have.** Changing the exposure mid-session invalidates the
dark, and a night rarely holds one exposure. A bias is valid for every one of
them, so it is what remains when the dark is refused.

Hence the rule, which is the one thing in the calibration path that is easy to
get wrong in a way nothing on screen reveals: **never both.** A dark contains
the bias; subtracting the two takes the pedestal off twice, the sky goes
negative and `np.maximum(f, 0)` turns it into a black floor with no signal in
it — a frame that looks calibrated, stacks, and carries nothing.
`calibrate` therefore takes the dark when its shape matches and the bias
otherwise, in that order, and the panel says which one is in force.

The rest of it followed from having three kinds instead of two: the naming
convention (`dark_g250_o20_e5.00s_bin2_-10C.fits`) existed in two copies, in
the worker and in the CLI, and a third kind would have made three. It is now
`core/masters.py`, along with `mismatch` — the readback of what the name
promises, header against camera, warning per field. Which fields matter is
per-kind and is the physics of each: the offset *is* the pedestal a bias
measures, the exposure and the temperature are what a dark integrates, and a
flat is a ratio that cares about neither.

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

### The compass offsets `alpha`, it never replaces it

`pointing/model.py`

The compass and the orientation event's `alpha` share no origin. On iOS
`webkitCompassHeading` reads from north while `event.alpha` counts from wherever
the page happened to load; the difference between them is arbitrary and can be
any angle at all.

`Pointing.feed` used to *substitute* the compass for `alpha` while unaligned and
then stop, which reads sensibly and is wrong in the worst possible way: the
alignment was solved against the compass reading, and every sample after it
arrived as the gyroscope's `alpha`. The sky jumped by the whole difference
between the two the instant the alignment reported success — **80 degrees in the
reproduction, with a 130-degree gyro origin**. Nothing announced it. The screen
said "aligned on Rigil Kentaurus" and the tube was in another part of the sky.

What it does now is keep one constant, `compass - alpha`, refreshed only while
unaligned. Before the first alignment the effective angle is still exactly the
compass reading, so the map is positioned as before; aligning stops the refresh
and freezes the constant, so the paragraph above becomes true instead of
intended. Two tests in `test_pushto.py` hold both halves: a handset with a
compass must point no worse than one without, and the map must still come up
oriented before aligning.

Every earlier test in this file passed `None` for the compass, which is why the
sensor was green in the test suite and useless in the field.

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

---

## The target ranking is a product of six factors, not a formula anyone tuned

`core/tonight.py`

The score is `altitude x window x moon x size x surface brightness x fame`, each
in 0..1 and each kept on the `Suggestion` so the panel can show it. That shape
was chosen so no single factor can rescue an object the others rule out — a
magnificent galaxy 12 degrees above a wall is not a target tonight — and so the
sentence explaining a score is always the same one.

What each factor is measuring, and why it is not something simpler:

- **altitude** is atmospheric extinction, `10^(-0.4·0.25·(X-1))`, not a linear
  ramp: it is the same physics that makes the object fainter, so the number
  means something. Above 80° it is multiplied by 0.7 — `pointing/pushto.py`
  already warns that there the azimuth is unstable and the Dobsonian awkward.
- **window** is minutes left above the requested altitude against the ~45 minutes
  a useful run takes. It also absorbs "is it rising", which is why there is no
  separate bonus for that.
- **moon** multiplies phase, proximity and the Moon's own altitude, weighted per
  family (`MOON_SENSITIVITY`). One number for all object types would be wrong in
  both directions: under a gibbous Moon a globular is still worth imaging and a
  face-on galaxy is not.
- **size** is the object against the short side of the frame. Larger than the
  frame keeps 0.4 rather than being dropped — a slice of the Veil is still worth
  a night.
- **surface brightness**, not integrated magnitude. M31 is magnitude 3.4 and
  computes to 22.2 mag/arcsec², which is exactly why it disappoints in EAA.
- **fame** is the uncomfortable one, and it earns its place empirically. With
  the other five alone, a real evening's first fifteen suggestions were fifteen
  anonymous open clusters — NGC 6743, NGC 6755, NGC 6664 — which score well
  because they are small, bright and moon-proof, and none of which is why anyone
  carries a telescope outside. A catalogue does not record what is worth
  looking at; whether anyone ever gave the object a name is the closest fact it
  has. Messier 1.0, named 0.92, catalogue number 0.7.

### The Moon and the planets are ranked on the same six factors

They are the targets no catalogue carries — a catalogue records what stands
still — so they arrive from `core/lucky.py`'s ephemeris and are scored into the
same list rather than beside it. A separate list would be the wrong shape for
the question the screen answers: at nine o'clock the choice is not "which
galaxy" but "Saturn or M8", and two lists cannot answer that.

Three of the six factors carry over unchanged, and mean the same thing:
altitude, remaining window, and surface brightness — the published mean
magnitude of a square arcsecond of disc walks straight into the catalogue's
formula and comes out saturated at 1.0, which is the honest answer for a body
fifteen magnitudes above the sky it is seen against.

The other three do not survive the crossing:

- **moon** is 1.0. Moonlight raises the sky background, and a planet is not
  competing with the sky background. The Moon does not shine on itself either,
  so the separation is `NaN` rather than 0° — a number that would be true and
  would still mean nothing.
- **fame** is 1.0. The factor exists to separate the objects somebody drove out
  for from the fourteen thousand that only have a number; every one of these is
  the former.
- **size** is not about the frame. Jupiter covers two hundredths of a percent
  of a bin1 frame, so `_size_factor` would score every planet like a distant
  smudge; what decides a planetary session is whether the image scale resolves
  the disc, so the factor is `BODY_PX` over the disc in pixels — Neptune is
  1.5 px at 1.53"/px, Mars at opposition 16, Jupiter 29, and the Moon runs off
  the top of the curve.

The cost is 55 ms of ephemeris against the 12 ms the whole catalogue takes,
which is why `lucky.bodies_at` computes the Sun once for the eight instead of
once per body (116 ms measured that way), why the lookahead that says which way
a phase is going is done for the Moon alone — it is the only body whose
`phase_name` reports it — and why the interface caches the eight by the minute
they were asked for: the four filters recompute the list, and the sky they read
from has not moved.

### Why the positions come from an hour angle formula and not from astropy

The list is recomputed on every filter change and every hour shift, over the
~12 thousand objects OpenNGC leaves after the "interesting" filter. Sidereal
time, the Sun and the Moon still come from astropy — that is three transforms.
The objects go through `sin(alt) = sin δ sin φ + cos δ cos φ cos H`, vectorised:
**3 ms for the whole catalogue** against the 9 ms astropy spends on the three
bodies it still computes, and the same formula also gives the transit and the
remaining window, which a per-instant transform does not. A full list refresh —
sky, ranking and table — measures 12 ms.

The cost is precession from J2000: measured against `pointing.sky_vectors` at
this epoch, **0.3° in altitude** (`test_tonight.py` asserts under 0.5°). The
altitude factor moves about 1% per degree, so a third of a degree cannot reorder
the list. Do not "fix" this by transforming every object — it buys nothing and
costs the hour slider.

---

## Focusing stopped being a mode

`ui/loupe.py`

FOCUS was one of four modes: a huge HFR, the trend plot, the beep and the 5x
loupe. In use the mode was opened for the loupe and left immediately, because
focus is not a phase of the night — it is something you redo whenever the
temperature drifts, in the middle of framing or of an integration, and leaving
the screen you were on to do it is the wrong trade.

So the loupe became a panel that floats over the image in any mode (`Z`), the
HFR it used to show large is the one the vitals bar already carried all night,
and the beep follows its own checkbox instead of the active mode. The trend plot
did not survive: the verdict sentence it existed to support ("improving",
"getting worse", the px/min figure) is in the loupe, and the plot itself was
being read by nobody with a hand on the focuser.

The freed slot went to TARGETS, which is what the night actually starts with.

---

## CONFIG stopped being a mode too

`ui/main.py` — `_top_bar`, `_build_config_window`

The same argument, from the other end. The rail's five buttons read as five
steps of a night, numbered, in order; the fifth was folder paths. Nobody
configures the export folder as a phase of an observing session, and being a
mode had a cost beyond the wrong teaching: entering it swapped the panel of
whatever you were doing for a settings screen, and it had to be written into
`can_integrate` — "config" counted as a stacking mode — so that opening it
mid-integration did not stop the stack.

So it moved to a window of its own, opened from a top bar (or the platform's
preferences key), which changes no mode, no view and no worker state; the stack
keeps integrating behind it. `can_integrate` went back to being one mode.

Two things stayed out of that window, on the top bar: night mode, image only and
the log. They are the only controls of the old CONFIG panel that get touched
*during* a night, and behind a window that has to be opened and closed they
would have ended up worse off than they started. The bar also stays on screen in
"image only" — hidden, the only way back would be the `F` key, with nothing on
screen to say so.

The freed slot in the rail went to nothing: four buttons in a 2x2 grid is what
the sequence actually is.

---

## The polar alignment is measured from the field rotation

`core/platform_align.py`, `core/polar.py`, `ui/main.py` — `_build_align_window`

`polar.py` has always been able to turn a platform axis into two screw
movements, and has never had an axis to turn: it wants solved positions and the
plate solver was removed. The rotation of the field, on the other hand, is
measured on every accepted frame already, and it is the same information.

The sky turns about the celestial pole `P`, the platform turns the tube about
its own axis `p`, and what the sensor sees is the difference: a slow rotation
about `e = p - P`. Split against the line of sight `u`:

- the part of `e` perpendicular to `u` moves the target across the frame — the
  drift, whose direction on the sensor nobody knows without a plate solve;
- the part **along** `u` turns the field about its own centre, at
  `sidereal rate x (e . u)`, a signed scalar that needs no orientation at all.

That scalar is what `PlatformMonitor` was already fitting for the rotation
budget. Two targets far enough apart give two equations for the two degrees of
freedom of `e`, and `ideal_next` is what stops the second target from being a
repeat of the first: the design matrix's weakest singular direction, projected
onto whatever is above the horizon.

### Hour angle, at the middle of the run — 3% and 13%

Writing that equation in right ascension is wrong in a way that looks right.
The platform is bolted to the ground: its axis is fixed against the horizon,
not against the stars, so `e` only stands still in a frame that turns with the
Earth. In equatorial coordinates the projection `e . u` drifts as the target
moves across the sky, and a straight line fitted over the run returns its
average, which belongs to the *middle* of the run and not to its start.

Measured against a simulated sensor (`tests/test_platform_align.py` turns the
Earth and the mount and reads the angle off a pair of stars), reading the rate
at the start of the run biases it by **3% over ten minutes and 13% over
forty**, and the bias does not shrink with a smaller error — it is first order
in the span, not second order in `e`. Evaluated at the hour angle of the run's
middle instead, the same simulation agrees to **0.05% even over forty minutes**.
So `Station` carries the sidereal time of its own middle, and the solve happens
in the hour-angle frame, rotating back to equatorial only at the end, for the
instant the screws are actually turned.

### What limits the precision, and what fixes the sign

Two things this cannot get from the maths.

The precision is bought with the baseline. A one degree error rotates the field
by 0.0044°/min, and the uncertainty of a fitted slope falls as `1/span` but only
as `1/√n` in the frame count — which is why the duration is the setting the
window offers and the frame rate is not. Five minutes of ordinary subs resolves
an error of roughly a quarter of a degree; ten resolves under a tenth. That is
far short of a careful drift align, and far more than enough for a platform set
by eye, where the error starts at one to three degrees and every degree removed
multiplies the rotation budget the stack already reports.

The sign cannot be derived at all. The rotation is measured in sensor
coordinates, and whether it agrees with the sky depends on the parity of the
optical train — an odd number of reflections mirrors the image and flips it.
`settings.platform_parity` carries it, `parity = 1` is fixed by the synthetic
sensor in the tests, and what settles it in the field is cheaper than counting
mirrors: correct, measure the same field again, and if the residual rotation
*grew* the sign was backwards, which is exactly what `verify` says. A backwards
correction does not leave the error alone, it doubles it, so the test is
unambiguous on the first try.

### A window, for one reason more than CONFIG had

Measuring a station takes minutes with the tube pointed at a field, and the
second station is a different field: the procedure sends you back to FRAME
halfway through, twice. A mode would have to be left to do that, taking its
readout and its progress bar with it. So it is a window, like the
configuration, and the measurement is a `request` on the worker rather than a
mode of it — it survives moving between FRAME and INTEGRATE, and runs off the
star lists the capture already produces: no accumulator, no disk, no platform
travel spent.

---

## Which star to align on is a computable question

`pointing/brightstars.py`

The sensor needs exactly one star, and choosing it was left entirely to the
user: 179 names down to magnitude 3, in the dark, with cold hands. Two failure
modes come out of that, and only one of them is obvious.

The obvious one is not finding a star you are sure of. The quiet one is aligning
on the **wrong star of a close pair** — the program then reports a confident
position that is a few degrees off, the arrow points somewhere plausible, and
nothing on screen says anything is wrong.

So `for_alignment` scores every visible candidate on four factors:

| factor | why it is there |
| --- | --- |
| distance to the target | dominant. One star corrects two axes, so the correction is local: `test_pushto` walks 25° from the alignment star with a 2.6° crooked mount and lands tens of arcminutes off. A magnificent star on the other side of the sky is a *worse* alignment than a second-magnitude one beside the target. |
| isolation | degrees to the nearest star within 1.5 mag of it. Beyond 6° no confusion is possible; below that the score falls, because this is the failure that produces a wrong answer rather than no answer. |
| altitude | flat between 30° and 65°, falling at both ends: the zenith is where the Dobsonian is awkward and the azimuth unstable (the same 80° `pushto.guide` warns about), and the floor is refraction plus the neighbour's wall — the user's own minimum altitude, never below 20°. |
| brightness | a tie-breaker. The magnitude limit already did the filtering, so the factor is flat from first magnitude up: Vega and Antares are both simply obvious. |

Measured: 6.7 ms for the whole ranking, one astropy transform for the 179 stars
plus a 179x179 separation matrix in numpy. It is recomputed every 30 s and
immediately whenever the target changes — never in the constructor, which used
to cost the window 570 ms of start-up against 203 now, for a label nobody was
looking at yet.

---

## The thumbnail is cached first and fetched second

`core/previews.py`

The pictures in TARGETS are DSS cutouts from the CDS. Everything about the
module is shaped by one fact: **there is no internet where the telescope is.**
So the disk cache is not an optimisation of the feature, it *is* the feature —
`fetch` looks at the cache before it looks at the network, the interface has a
button that caches a whole list while there is still wifi, and being offline is
a normal state that produces "no picture", never an error.

Three details that are not obvious:

- **the cutout is framed on the object, not on the sensor.** The first version
  sized it to the field, which is what you would do if the picture existed to
  show framing. It made every small target useless: M57 is 1.3' inside a 51'
  field, so the picture was a black square with a dot. Now the cutout is a
  little over twice the object, and the sensor's rectangle is drawn *on top*.
- **the rectangle is dropped when it does not fit.** For a small object the
  frame is wider than the whole cutout, and a rectangle clamped to the edges is
  not a rectangle, it is four lines outside the picture. When the field is wider
  than the cutout the answer — "it fits with room to spare" — is already in the
  reasons line.
- **the cache key is name *and* field.** The same object at two zoom levels is
  two pictures, and changing binning or focal length has to invalidate the old
  framing rather than quietly show a rectangle that no longer means anything.

Measured: ~1.8 s to fetch, 0.1 ms from cache, ~30-60 KB per object. Two
concurrent workers, not more: the CDS runs that service for everyone.

The rectangle is the whole reason the picture is there. "M8 is 45 arcminutes"
and "the frame is 51x29" are two numbers nobody composes in their head at
midnight; the same fact drawn on the real sky is instant.

---

## Text never decides the window's width

`ui/design.py` (`ElidedLabel`)

A `QLabel` reports the width of its whole text as its **minimum** width, and Qt
satisfies a layout's minimum by resizing the *window*. So a readout that grows
with the night was enough to drag the window wider than the screen and push its
right edge out of sight, with no way to get it back other than dragging the
title bar.

Measured on the offscreen build, as the minimum width of the central widget:

| what changed | before | after |
| --- | --- | --- |
| idle, FRAME mode | 1334 px | 1271 px |
| opening TARGETS (the summary line: "60 suggestions for … · the Sun is up") | 2082 px | 1271 px |
| the exposure phase going from "EXPOSURE" to "READING AND PROCESSING  +7.9s" | 1364 px | 1271 px |
| a reviewed frame's line, with the five measurements and their rulers | +1500 px | 1271 px |

The phase one is why the window also grew *between frames*: the text changes on
every frame, so the window widened and narrowed on its own all night.

`ElidedLabel` gives the decision back to the layout — it elides what does not
fit, keeps the full string in the tooltip, and answers `text()` with the full
string so the elision is only what gets painted. Three labels use it: the view
bar's line, the exposure phase and every `Stat` value (an object's full name in
the 22 pt display font asks for ~600 px on its own).

Two things that were **not** the fix:

- **word wrap.** It caps the minimum at the longest *word*, which is no cap at
  all for a filename or a catalogue designation — and it steals height from the
  image to buy width.
- **a maximum width on the column.** The left column has had `setMaximumWidth`
  since the beginning and the window grew anyway: `QSplitter` adds up its
  children's minimum hints, and a maximum does not lower a minimum.

The remaining 1271 px floor is the vitals card (1255 px), which is a row of
fixed-width readouts and not text-driven. `tests/test_gui_window_width.py`
holds the floor: it fails if any mode, view, long readout or arriving frame
moves it.

## The lucky path is fast because it draws less, not because it computes less

PLANETS runs at the frame rate of a millisecond exposure, which is two orders
of magnitude more often than a deep-sky sub. Everything the deep-sky path does per
frame without noticing becomes the bottleneck. Measured on one bin1 frame
(2822 x 4144 = 11.7 MP, RGB float32 = 140 MB):

| stage | before | after |
| --- | --- | --- |
| calibrate + luminance + levels + sharpness | 35 ms | 35 ms |
| demosaic + float RGB (display only) | 42 ms | only on a drawn frame |
| `_quantize` | 50 ms | 50 ms |
| linear stretch, neutral | 384 ms | 67 ms |
| saturation at 2.5 | 268 ms | 140 ms |
| **worker, per frame** | **77 ms** | **35 ms** (31 fps ceiling) |
| **GUI, per drawn frame** | **552 ms** | **117 ms** |

Three things were wrong, in order of how much they cost:

**The display ran at the capture rate.** The `frame` signal crosses threads, so
it is queued and unbounded. At 30 fps in and 2 fps out, the queue grows by a
140 MB frame roughly every 30 ms — the process is swapping within seconds, which
is what "slow" actually looked like. The fix is a ceiling in
`CaptureWorker._due_for_display` (`Settings.lucky_display_fps`, 12 by default):
every frame is measured and recorded, only some are drawn. It also means the
demosaic and the float conversion — 42 ms, and for the screen only — happen on a
drawn frame instead of on every frame, so the branch out of `_process` had to
move above them.

**The stretch worked on the image instead of on a table.** The balance,
the white point and the gamma are each a function of one input value, so all
three fold into one 65536-entry table per channel, applied to the uint16 frame.
The result is bit for bit identical and 5.7x faster. This is the same trick
`stretch.build_lut` already did for the deep-sky path; the lunar path was
written the obvious way first, which is 384 ms of `x ** gamma` on 35 million
floats. Saturation cannot fold in — it couples the channels — so it stays a
second pass, moved to fixed-point int32 in `stretch.saturate_u8`: 140 ms against
268 ms, never more than one level apart. The night-mode LUT indexes the green
channel rather than the mean of the three, which was allocating a float64 copy
of the frame for 94 ms.

**RICE was compressing the burst.** 119 ms a frame against 32 ms, which caps the
burst at 8 fps instead of 31 for 40% less disk. A deep-sky session writes one
sub every few seconds and should compress; lucky imaging is bought in frames and
should not, so `Settings.lucky_burst_compress` defaults to off and the choice
sits in that panel rather than being inherited from INTEGRATE.

What is *not* fixed: `_quantize` still converts the whole float RGB frame to
uint16 (50 ms) so the LUT has something to index. Removing it means the lucky
path emitting uint16 RGB and `_src()` no longer meaning "float in 0..1" for
everyone. The larger win in the same direction is a camera ROI: the disc is a
third of the frame at 1200 mm and two hundredths of a percent of it for a
planet,
`Camera.set_roi` already takes x/y/width/height, and cropping to the disc would
divide every row of the table above — and the USB readout with it. The window
described below is that crop done in software: it makes the *measurements* mean
the body, but every pixel is still read off the sensor and still travels through
the display path.

## The pedestal is worth more than the sharpening

Two things sit under every pixel of a lunar frame and neither is Moon: the
sensor's offset, and light scattered in the sky and the tube. Both are additive,
so until they come off, no colour ratio in the image means anything.

Measured on the eclipse of 28 Aug 2026 — 20 frames, bin1, 100 ms, gain 250, no
dark and no flat:

| | with the pedestal | without |
| --- | --- | --- |
| sky floor | 0.0203 of full scale (1330 ADU) | 0.00005 |
| disc contrast, p95/p5 | 7.2x | 10.5x |
| umbra R/G | 1.284 | **1.361** |
| umbra B/G | 0.754 | **0.689** |

A third of the eclipse's colour was being diluted by a 2% grey veil. That is why
`lucky_stack.stack` subtracts it by default and `--no-background` is the opt-out,
while sharpening — the step everyone reaches for first — is off unless asked.

Per channel, not one number: the offset is common to the three but the scattered
light and the channel responses are not, and that difference is exactly what the
subtraction recovers.

A constant, not a fitted surface. Across the corners of the same frame the
pedestal varied by 11% of itself, which is 0.2% of the disc — a plane fit would
be modelling something two orders of magnitude under the signal. The right fix
for that variation is a flat, before the fact, not a surface afterwards.

It comes off the finished stack rather than off each frame: it is the same
constant either way, and measuring it once on an image with a tenth of the noise
is the more stable of the two.

## The histogram's scale comes from the sky, not from the brightest pixel

`_draw_histogram` set its right edge to the maximum of the sampled pixels.
Measured on an 8 s sub of the Veil at bin2:

| | |
| --- | --- |
| top of the axis | 0.5726 of full scale |
| sky median | 0.0797 → bin 35 of 256 |
| 99.9% of pixels below | 0.0952 → bin 42 |
| pixels above that | 137 of 137529 |

One part in a thousand was taking 84% of the plot, and everything the readout
exists to show — the sky peak, its shoulder, where the black point falls — was
crammed into the leftmost sixth. A single bright star decided the axis of the
one instrument used to judge every other frame of the night.

A high percentile is not enough on its own. This camera delivers a few
hundredths of a percent of hot pixels — 4738 of them on that frame, 0.041% —
which is exactly the population a 99.99th percentile lands in. What cannot be
moved by them is the median, so the scale is anchored there: the sky peak at a
third of the width (`HISTOGRAM_SKY_SPAN`), with the 99.9th percentile only
widening it when the bright tail genuinely reaches further. On the same frame
that gives 0.24, with the sky at bin 85.

Saturation overrides both. The histogram is also how you see that you are
clipping, so when more than `SATURATED_FRACTION` of the frame sits at the top of
the scale the plot goes to full range and the wall against the right edge stays
visible. A handful of hot pixels does not qualify — that is the case the whole
function exists to ignore.

Two things the histogram cannot do, both asked in the field. It cannot show
banding: on that frame the row noise was 0.000188 of full scale, a twelfth of
one bin, and more fundamentally a histogram discards position — an image with
stripes and the same pixels shuffled at random have identical histograms. And it
cannot show the stretch, deliberately: the display curve is left out so the plot
keeps meaning something for judging exposure.

## Post-processing order, and what each step is correcting

`core/postprocess.py`

`process()` runs gradient removal, atmospheric-dispersion alignment, colour
calibration, optional deconvolution, the stretch, then chroma/luminance denoise
and saturation — always in that order, because the first three are only valid
on linear photon counts and the stretch is the one non-linear transition. Doing
gradient or colour on stretched data produces the blotchy, off-colour look that
sends people back to redo the whole thing.

Measured on `sessions/2026-08-25/2035_M6/stack_215606_242f_20m10.fits`: 242
frames x 2 s = 20m10, gain 400, bin 2, M6 at roughly 60° altitude, no dark, no
flat.

**Gradient is a tile grid, not a polynomial.** The dome measured there peaks
off-centre at ~(0.6w, 0.25h) and falls 45% to the bottom corners — vignetting
from an uncorrected Newtonian plus light pollution on top. A degree-2
polynomial fits a symmetric paraboloid and leaves that asymmetry behind as a
visible veil; a tile grid follows whatever shape the sky actually has, at the
cost of needing enough star-free tiles (`star_mask` is what keeps the grid off
stellar haloes in a dense field, not just off the stars themselves).

**Atmospheric dispersion is real and separate from chromatic focus, and needs
two different fixes.** The atmosphere is a prism: at ~60° altitude the R and B
centroids sat 0.44 and 0.36 px from G, in *opposite* directions — 0.8 px end to
end. That is a centroid shift, so `align_channels` (a rigid shift per channel)
removes it. Chromatic focus is different: R measured 5.9% broader than G and B
5.3%, a difference in PSF *width*, which is symmetric around a star and
therefore invisible to a centroid shift — `match_psf` is a separate,
opt-in step because matching it means blurring the sharper channels down,
trading resolution the seeing may not have made worth it (there, luminance FWHM
moved from 4.02 to 4.26 px, 4%, at 1.59"/px seeing that was never resolving that
finely anyway).

**Colour calibration needs a robust average.** M6 is a young open cluster: its
brightest members are hot blue B stars, and there the top 3% of stars by flux
carried 27.8% of it. Averaging by summed flux (`color_method="flux"`) lets that
3% force the gain, which measured as a 14.8% error in the red channel and left
88% of the field's stars looking red against a handful of blue ones. The median
of each star's own R/G and B/G ratio (`color_method="median"`, the default)
weights every star equally and cannot be dragged by a few bright outliers —
neither is truth without catalogue colours, but the median is the one a handful
of stars cannot decide alone.

**The stretch needs ONE black point for all three channels, not three.**
`stretch.auto_arcsinh` picks a per-channel black point at a fixed number of
sigmas below that channel's own noise floor, and sigma is not equal across
channels: a Bayer sensor has twice as many green photosites, so G is quieter by
`sqrt(2)` — measured there, MAD 5.4e-5 in G against 6.5e-5 in R and 7.0e-5 in B.
A background that was neutral in linear data (R/G = B/G = 1.000, thanks to
colour calibration above) came out of the per-channel estimator at R/G 1.20,
B/G 1.29 — magenta, purely from the estimator, not the sky. `arcsinh_neutral`
solves the black point once, on luminance, and applies it to all three.

**A colour ratio needs a noise floor, or it turns background noise into
blotches.** Preserving colour (Lupton et al.) means every channel follows the
stretched luminance by its own ratio to the pre-stretch value, `b / bl`. That
is a ratio of two small numbers wherever `bl` sits near the noise floor, and
the `bl > 1e-8` guard that stops it dividing by exact zero does nothing for
real noise, which sits many orders of magnitude above 1e-8. The result,
measured on `sessions/2026-09-01/1946_NGC6995`: a partial-coverage corner with
ordinary, uniform-looking noise turned into clumped red/blue blotches after
the stretch, and no amount of denoising afterwards separated them back out —
denoising a signal that is already colour-correlated by construction just
smooths the blotches, it cannot un-correlate them. `arcsinh_neutral` blends
the ratio down to neutral (1.0) below six sigma of the channel's own noise,
using the same `mad` the black point was solved from. Real colour — a star,
the nebula — sits far enough above that floor to pass through untouched; only
the sky's own noise is desaturated, which is also the physically honest answer
since noise does not have a colour to preserve.

The same measurement showed a second, smaller contributor: `calibrate_color`'s
gain is measured on stars and applied flat to every pixel, sky included, which
multiplies whichever channel is noisiest by the same factor that corrects a
star's colour — invisible on the star, visible as extra chroma noise on blank
sky. It is now blended down to a gain of 1 (no change) below four sigma of the
frame's own noise the same way, for the same reason: a gain measured on
signal has nothing to correct on a pixel that has none.

**The median gain assumes the field averages to neutral, and a heavily
reddened sightline is the one place that is false.** Measured on
`sessions/2026-09-06/1834_NGC6441/stack_193458_228f_26m35.fits`: 228 frames x
7s = 26m35, NGC6441 at 55-59° altitude (airmass 1.16-1.22, so not atmospheric
extinction) but at galactic b=-5° — close enough to the plane that most of the
395 detected stars carry the same real interstellar reddening, G Scorpii (the
naked-eye G8 giant 7' from the cluster) included. `calibrate_color` measured
R0.669/G1.025/B1.616: sampling the linear data directly in rings around G
Scorpii before this step gave R>G>B (a warm halo, consistent with a yellow
giant); after it, the same rings came back B>G>R — the gain had not corrected
a sensor error, it had inverted the scene's own colour, most visibly on the
brightest pixels because the noise-floor blend above is *strongest* exactly
there. `COLOR_GAIN_LIMIT = 1.25` caps the per-channel gain to what a sensor
residual plausibly looks like — on M6 (14.8% in the red channel) the cap never
engages; here it holds the swing to R0.8/B1.25.

That cap alone still left a visible blue ring in the star's own diffuse halo,
sampled as a patch off to one side rather than averaged around the full ring
— the ring average washes an asymmetric patch out, which is why the first
pass missed it. The halo measured 9.15 sigma above the background MAD,
against 3.76 to 806 (median 53) for the star cores the gain was actually
measured on: `calibrate_color`'s noise-floor blend was gating on any signal
above 4 sigma, so this in-between brightness — well above sky, well below a
real star — got the full gain as if it were one of the stars the gain
represents, and the frame's own uniform reddening (the halo's pre-calibration
ratio was no different from plain sky's) overshot past neutral into blue
exactly there. Raised to `COLOR_WEIGHT_FLOOR_MAD = 20.0`, the halo's weight
drops enough that the ratio comes back to R/G 1.01, B/G 0.98 — neutral, not
blue — while the median star core (53 sigma) still clears the floor and keeps
its correction.


## A planet is not a small Moon: everything is measured in a window

The mode was written for the Moon, where the body is a third of a bin1 frame,
and every measurement in it was taken over the whole frame. Both of them break
on a planet, and neither breaks loudly.

`focus.sharpness` is the mean squared gradient divided by the mean squared
level — normalised that way so that turning up the gain does not read as better
focus. The normalisation removes the exposure; it does not remove how much of
the frame is dark sky. Jupiter at 1200 mm is about 57 px across in an 11.7 MP
frame: two hundredths of a percent of its pixels. What that ratio then reports
is the
contrast of the sky noise, which does not change when the focuser moves.

`lucky.levels` counts the fraction of the frame above a sky floor and calls
anything under half a percent "nothing bright in the frame". A planet is two
orders of magnitude below that threshold, so the exposure guard would have
declared every planetary frame empty while the disc sat there clipping.

So both are measured inside `lucky.window`: a square around the body, found on
the half-resolution luminance by thresholding at a quarter of the way from the
sky to the peak. Three things about it are load-bearing:

- **The size is fixed after the first frame; only the centre follows the body.**
  The window's area is the denominator of both measurements. A window that
  resized itself with the seeing would move the sharpness readout while the
  focuser stood still — the one number you watch when nothing else is supposed
  to be changing.
- **What is over the threshold has to look like a body.** On an empty frame the
  threshold selects noise, which reaches every corner and fills its own bounding
  box about a fifth. A disc fills three quarters of the box around it. The test
  is only applied to something that already spans the frame, because a crescent
  fills a third of its box and is unmistakably a body wherever it is small.
- **It is found on the luminance and scaled to the mosaic with an even origin.**
  The frame the exposure is measured on is a Bayer mosaic; an odd origin shifts
  the pattern and every colour in the crop is then the wrong one.

The same window is what `lucky_stack` stacks in, with one addition: each frame
is centred on its own centroid before the phase correlation runs. The body
crosses the frame over half a minute — on an equatorial platform by a few
hundred pixels — and a correlation only sees a shift that is small against its
own window. Measured on a synthetic burst with three frames 30 px apart:
aligned, the stack keeps the peak of a single frame (0.499 against 0.502);
averaged where they fell, it keeps 58% of it. Both write a stack.

A body wider than half the frame's short side is not cropped to — there the
frame *is* the body, and the crop would throw away the limb the alignment reads
for a window barely smaller than what it came from.

## The exposure of a planet is the Moon's, scaled by surface brightness

Eight bodies with three capture settings each is eight sets of numbers to keep
true, and seven of them would be guesses. What actually transfers between two
targets under the same optics and the same sensor is the surface brightness: a
body is not brighter to expose for because it is bigger, it is brighter because
each square arcsecond of it is. So `Settings.lucky_exposure_s` holds the Moon's
exposure — the one anybody starts a night with, and the one worth measuring —
and `lucky.exposure_for` scales it by `10 ** (0.4 * Δmag/arcsec²)`.

From the Moon's 3.4 mag/arcsec², that is 0.25x for Venus, 0.6x for Mercury,
1.3x for Mars, 6.3x for Jupiter and 25x for Saturn. The last two run into the
other constraint: the
atmosphere rearranges itself in tens of milliseconds, so a frame longer than
that averages two atmospheres together and there is no sharp frame left in the
burst for lucky imaging to pick. The exposure therefore stops at
`lucky.FREEZE_S` (20 ms) and the shortfall is reported as a factor the gain has
to make up, rather than quietly taken in time: from an 8 ms Moon that is 2.5x
for Jupiter, 10x for Saturn and 28x for Neptune.

The gain is not scaled automatically for the same reason the surface
brightnesses are only good to a few tenths: nothing here has measured what this
camera's gain units do to the signal, and inventing that conversion would be
inventing the one number the exposure guard is there to settle by measurement.


## Holding the body still is a move of the view, not of the frame

At the magnification a planet is worth watching at, wind and seeing walk it
across a good part of the screen. Focusing by eye on something that will not
stay still is guesswork, and the wobble reads as a mount problem when it is the
atmosphere.

The obvious implementation is the wrong one. Shifting the *pixels* so the body
lands in the middle costs a `warpAffine` of an 11.7 MP RGB frame on every drawn
frame — the display path is already the expensive half of this mode, at 117 ms
a frame — to correct something `lucky_stack` corrects for free afterwards, and
it puts an interpolation between the eye and the focus it is judging. The frame
would also stop being the frame: the histogram, the loupe and the exposure guard
all read what is emitted.

So what moves is the rectangle being looked at. `lucky.centroid` runs on each
drawn frame, the worker reports the body's position in the recorded frame's own
pixels, and the GUI re-ranges the ViewBox around it at whatever zoom is set. The
frame arrives untouched, and turning the following off leaves the view exactly
where it was.

Two details make it usable rather than merely correct. Switching it on zooms to
the body if the whole frame is on screen — panning a fitted view moves nothing,
so without that the button would look broken. And `fit_view` switches it off,
because "show me the whole frame" and "follow one body inside it" are opposite
requests, and the next drawn frame would otherwise undo the fit.

The centroid is sampled, not read whole: on a full-disc window of a bin1
luminance frame (1090 x 1090) reading every pixel costs 37 ms against 1.9 ms
with `lucky.tracking_stride`, and the two centres differ by 0.23 px — a quarter
of a pixel, against the tens the wind moves it by.
