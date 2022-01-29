# mp-pico-ra-calculator

[![lint](https://github.com/alanbchristie/mp-pico-ra-calculator/actions/workflows/lint.yaml/badge.svg)](https://github.com/alanbchristie/mp-pico-ra-calculator/actions/workflows/lint.yaml)
![GitHub tag (latest SemVer)](https://img.shields.io/github/v/tag/alanbchristie/mp-pico-ra-calculator)
![GitHub](https://img.shields.io/github/license/alanbchristie/mp-pico-ra-calculator)

![Platform](https://img.shields.io/badge/platform-micropython-lightgrey)

A Right Ascension (RA) real-time compensation calculator.

Given a target RA and calibration date this code displays the compensated
RA value for a telescope with a fixed RA-aligned axis.

It is designed to run on a Raspberry Pi [Pico] with the assistance of I2C-based
[Real-Time Clock] module, a pair of [dot-matrix] displays, and a 32KByte [FRAM]
memory module.

It's essentially a handly real-time implementation of the 'trick' described in
my related repository on portable, battery-operated hardware that can be used
in the field: -

- https://github.com/alanbchristie/ra-converter

---

[dot-matrix]: https://shop.pimoroni.com/products/led-dot-matrix-breakout?variant=32274405621843
[fram]: https://shop.pimoroni.com/products/adafruit-i2c-non-volatile-fram-breakout-256kbit-32kbyte
[pico]: https://shop.pimoroni.com/products/raspberry-pi-pico?variant=32402092294227
[real-time clock]: https://shop.pimoroni.com/products/rv3028-real-time-clock-rtc-breakout
