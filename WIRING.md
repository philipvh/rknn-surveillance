# PIR contact → Rock 5B

The PIR already switches the floodlights and gives a dry contact, so this is
the simple version: contact between a GPIO line and ground, internal pull-up
on, closed contact reads low.

```
   PIR relay
   (volt-free contact)
        ┌───────┐
   ─────┤  NO   ├───── 1kΩ ───┬───────  GPIO line   (3.3 V, pull-up enabled)
        │       │             │
   ─────┤  COM  ├─────────────┼───────  GND
        └───────┘             │
                            100nF
                              │
                             GND
```

The 1 kΩ limits current if the line is ever mis-configured as an output; the
100 nF absorbs contact bounce so the software debounce has less to do.

## Before connecting anything

Confirm with a multimeter that the contact is genuinely volt-free and isolated
from the switched live side of the lighting circuit. It almost certainly is on
a relay output, but the Rock 5B's GPIO is **3.3 V and not 5 V tolerant**, and
the consequence of being wrong is a dead board.

If the run out to the sensor is long or leaves the building, put an
optocoupler between the cable and the header. A long outdoor pair is an
antenna for surges and the header connects more or less straight to the SoC.
For a short indoor run, skip it.

## Finding the line number

```sh
gpiodetect                      # which chips exist
gpioinfo                        # which line is your header pin
gpiomon gpiochip4 17            # watch it change — wave at the sensor
```

Put the chip and line into `trigger_input` in `config.yaml`, then:

```sh
./trigger_cli.py doctor         # can we open it, and what does it read now
./trigger_cli.py watch          # live events, with durations
```

If `doctor` says the line is ACTIVE while nobody is near the sensor,
`active_low` is set the wrong way round.

## What the software does with it

Polled 20×/second, not edge-driven: libgpiod's edge API changed incompatibly
between v1 and v2, reading a value did not, and a PIR's timescale is seconds.

* A change must hold for `debounce_s` (0.2 s) before it counts.
* Activations shorter than `min_active_s` (0.3 s) are discarded as blips.
* The signal is a **level**: the release event carries how long the PIR held
  the lights on, which is information about what was outside.
* If the line stays active for `stuck_after_s` (30 min) it warns once — a
  stuck relay or a light left on, not a fresh trigger.
* No usable GPIO is never fatal. It logs and carries on; detection still runs
  on the parked view.
