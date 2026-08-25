# SVBONY SV405CC — measured behaviour

Everything here was **measured** on this camera (fw v2.0.0.6, SDK v1.13.4,
macOS arm64), not read in documentation. Each item is a trap that would corrupt
a stack silently.

## Identification

|                      |                                                     |
| -------------------- | --------------------------------------------------- |
| Sensor               | IMX294, 4144 x 2822, 4.63 um pixel                  |
| ADC                  | 14 bits                                             |
| Colour               | yes, Bayer **GRBG** (row 0 = G R, row 1 = B G)       |
| Bins                 | 1, 2, 3, 4                                          |
| Formats              | RAW8, RAW16, Y8, RGB24                              |
| Cooler               | yes, target -40 to +30 C                            |
| Gain                 | 0 to 570                                            |
| Exposure             | 36 us to ~2000 s                                    |
| Offset (BLACK_LEVEL) | 0 to 80                                             |

## 1. RAW16 carries the 14 bits shifted 2 to the left

Real scale **0..65532**, values always multiples of 4. Measured from the minimum
step between distinct values (= 4) with a neutral white balance.

Treating it as 0..16383 makes everything 4x brighter; treating 65535 as true
saturation gets the clipping point wrong. Use `Camera.full_scale`.

## 2. The SDK applies white balance to RAW data — switch it off

With `WB_R = 400`, the R phase rose from 724.8 to 2802.0 (about 3.9x). It is a
direct multiplication over the Bayer mosaic: it breaks per-channel linearity,
invalidates flats and destroys any colour calibration. **128 = unit gain.**

Side effect: with WB active the quantisation grid of 4 disappears, which masks
item 1.

Bonus: this is how the Bayer pattern was confirmed — `WB_R` moved exactly phase
(0,1), consistent with GRBG.

## 3. Hot pixel correction ships ENABLED

`BAD_PIXEL_CORRECTION_ENABLE = 1`, threshold 60. It replaces isolated pixels
above the threshold — and a faint star occupying a few pixels is exactly that.
Disabled in `_force_linear()`; we build our own map from the darks.

## 4. Gain is locked while exposure is on auto

With `EXPOSURE` at `auto=1`, the `AUTO_TARGET_BRIGHTNESS` loop owns the gain and
`SetControlValue(GAIN)` returns `GENERAL_ERROR` (16). The camera **persists**
that flag between sessions, so the symptom looks intermittent.
`_leave_auto_mode()` reasserts the current value with `auto=0` on open.

## 4b. The SDK persists parameters to disk, and it bites

By default the SDK writes the camera parameters to a `U3SM*_Cfg_*.bin` file in
the **process working directory** and restores them on the next open.

That is the root cause of item 4. The camera came back from an earlier session
with auto exposure, the gain was locked, and the symptom looked random because it
depended on how the previous session had ended.

`Camera.open()` disables it with `SVBSetAutoSaveParam(id, 0)`. Everything is
reasserted explicitly anyway, and hidden state that outlives the process only
makes debugging harder. Welcome side effect: it stops littering the directory.

## 5. Reopening requires re-enumeration

After `SVBCloseCamera`, the `CameraID` is only accepted again once
`SVBGetNumOfConnectedCameras` / `SVBGetCameraInfo` have been called. Without
that, the second `SVBOpenCamera` of a process returns `INVALID_INDEX` (1).

## 6. Binning is software, on the host — and it is Bayer-aware

Measured: bin1 and bin2 take **the same time** (~524 ms), despite bin1 moving 4x
more bytes. The whole frame always crosses the USB; the SDK bins afterwards.

**Consequence: binning does not speed anything up.** Choosing a bin is purely a
scale/SNR decision.

The CFA is preserved — normalised phase vectors deviate 0.26-0.29% between bins,
which is noise level. It is a sum of same-colour pixels (means scale 4.0x and
9.0x), not an average, and not a blind sum over the mosaic.

## 7. bin3 and bin4 clip; bin2 is exact

The sum saturates at 0xFFFF. Since each value already occupies 14 bits shifted:

| bin   | max sum               | fits in 16 bits?           |
| ----- | --------------------- | -------------------------- |
| 1     | 65532                 | yes, exactly               |
| **2** | 4 x 16383 = **65532** | **yes, exactly — lossless** |
| 3     | 9 x 16383 = 147447    | **no, clips**              |
| 4     | 16 x 16383 = 262128   | **no, clips badly**        |

Measured at 0.8 s: bin3 with 14% of its pixels at 65535, while bin1 read
p99.9 = 7040 (11% of range). **bin3 loses about 3.2 stops of highlights.**

-> **Use bin2.** It is the only bin >1 that is mathematically lossless.

## 8. USB negotiated at 2.0 — change the cable

`PortType = USB2.0`, measured throughput 44.6 MB/s (the practical ceiling of
USB 2.0). That results in **about 475 ms of fixed dead time per frame**, at any
bin.

With 5-10 s subs that is 5-9% lost, which is tolerable. On USB3 the 23.4 MB would
cross in about 70 ms. Worth changing the cable before any software optimisation.

## 9. Changing bin/ROI needs slack

The first frame after `SetROIFormat` can take much longer than exposure plus
overhead (it returned `TIMEOUT` at 4 s). Rule: discard the first frame and use a
generous timeout after any geometry change.

## Still to measure (needs darkness and a capped sensor)

- **The HCG step.** The IMX294 has an abrupt read-noise drop somewhere in the
  0..570 range. On ZWO's scale for the same sensor it sits around 120 — a
  hypothesis to confirm with pairs of darks at neighbouring gains. It defines the
  EAA preset. `astrodoro sensor` runs this survey.
- Amp glow as a function of exposure and temperature.
- The minimum offset that avoids truncating the left tail of the noise (0 clips).
