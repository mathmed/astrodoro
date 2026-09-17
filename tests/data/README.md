# Test data

## `ngc-sample.csv`

Twenty-six rows taken verbatim from [OpenNGC][openngc], with its header intact,
so `core.catalog.Catalog.load` parses exactly what it parses in the field.

The suite used to reach for the real `data/NGC.csv`, which is downloaded and
never versioned: on a fresh checkout — and in CI — `search("M8")` found nothing
and the failure looked like a bug in the search. The objects here are spread
from 0h to 22h in right ascension and from -72° to +69° in declination, so
something is always above the horizon whatever hour and hemisphere the tests run
at.

`astrodoro catalog` is still what a user runs; this file is only for the tests.

Source: Mattia Verga, OpenNGC, **CC BY-SA 4.0**. See `docs/third-party.md`.

[openngc]: https://github.com/mattiaverga/OpenNGC
