"""The real-time RA compensation calculator.

Designed to run on a Raspberry Pi Pico using the
Pimoroni customised MicroPython image.
"""

# Tested with: -
#
# - pimoroni-pico-v1.18.1-micropython-v1.18.uf2

import time
try:
    from typing import Dict, List, NoReturn, Optional, Tuple, Union
except ImportError:
    pass

import micropython  # type: ignore
from machine import I2C, Pin, Timer  # type: ignore
from ucollections import namedtuple  # type: ignore
from pimoroni_i2c import PimoroniI2C  # type: ignore
from breakout_rtc import BreakoutRTC  # type: ignore

# _RUN
#
# Setting this to False will avoid automatically running the application.
# Setting to False is useful because you get all the
# global objects and can manually configure/erase the FRAM
# and set the RTC module.
#
# If the application is running you can press and hold the "UP" button
# (button 4) for a long-press to cause the application to stop,

_RUN: bool = True

# To erase our section of the FRAM run: -
#
#    _RA_FRAM.clear()
#
# We do not use the MicroPython built-in RTC class instead we
# use the RV3028 I2C module and, before running the code for the first time
# we need to set the initial time. The RV3028 is extremly low power
# (45nA at 3V) and is factory calibrated to +/-1ppm at 25 degrees
# (a drift of 1 minute every 23 months).
#
# You can get the current RTC date and time with: -
#
#    _RA_RTC.datetime()
#
# To set time and date to "14:46:25 7-Feb-22", a Monday,
# which is day 1 at our application level, day 0 in the RV3028: -
#
#    _RA_RTC.datetime(RealTimeClock(2022, 2, 7, 1, 14, 46, 25))
#
# If you haven't set the RTC you can use our RTC class by setting the _RUN
# variable to False. This code can then be loaded (by something like Thonny)
# and you'll have access to the objects described above to clear the FRAM
# and set the date and time.

# Uncomment when debugging callback problems
micropython.alloc_emergency_exception_buf(100)

# An RA value: hours and minutes.
RA: namedtuple = namedtuple('RA', ('h', 'm'))
# A Calibration Date: day, month
CalibrationDate: namedtuple = namedtuple('CalibrationDate', ('d', 'm'))
# A Real-Time Clock value
# Year is full year, e.g. 2022
RealTimeClock: namedtuple = namedtuple('RealTimeCLock', ('year',
                                                         'month',
                                                         'dom',
                                                         'dow',
                                                         'h',
                                                         'm',
                                                         's'))

# The target RA (Capella, the brightest star in the constellation of Auriga).
# This is the Right Ascension of the default target object.
DEFAULT_RA_TARGET: RA = RA(5, 16)

# The date the telescope's RA axis was calibrated.
# We don't need the calibrated RA axis value, just the day and month
# (where 1==January) it was calibrated.
DEFAULT_CALIBRATION_DATE: CalibrationDate = CalibrationDate(3, 1)

# Minutes in one day
_DAY_MINUTES: int = 1_440

# What constitutes a 'long' button press?
_LONG_BUTTON_PRESS_MS: int = 2_000

# The period of time to sit in the
# button callback checking the button state.
# A simple form of debounce.
_BUTTON_DEBOUNCE_MS: int = 50

# Configured I2C controller and its GPIO pins
_I2C_ID: int = 0
_SDA: int = 16
_SCL: int = 17

# A MicroPython I2C object
_I2C: I2C = I2C(id=_I2C_ID, scl=Pin(_SCL), sda=Pin(_SDA))

# Find the LED displays (LTP305 devices) on 0x61, 0x62 or 0x63.
# We must have two. The first becomes the left-hand pair of digits
# and the second becomes the right-hand pair.
_DISPLAY_L_ADDRESS: Optional[int] = None
_DISPLAY_R_ADDRESS: Optional[int] = None
_DEVICE_ADDRESSES: List[int] = _I2C.scan()
for device_address in _DEVICE_ADDRESSES:
    if device_address in [0x61, 0x62, 0x63]:
        if not _DISPLAY_L_ADDRESS:
            # First goes to 'Left'
            _DISPLAY_L_ADDRESS = device_address
        elif not _DISPLAY_R_ADDRESS:
            # Second goes to 'Right'
            _DISPLAY_R_ADDRESS = device_address
    if _DISPLAY_R_ADDRESS:
        # We've set the 2nd device,
        # we can stop assigning
        break
assert _DISPLAY_L_ADDRESS
assert _DISPLAY_R_ADDRESS
print(f'L display device={hex(_DISPLAY_L_ADDRESS)}')
print(f'R display device={hex(_DISPLAY_R_ADDRESS)}')

# Do we have a Real-Time Clock (at 0x62)?
_RTC_ADDRESS: Optional[int] = None
if 0x52 in _DEVICE_ADDRESSES:
    _RTC_ADDRESS = 0x52
if _RTC_ADDRESS:
    print(f'RTC  device={hex(_RTC_ADDRESS)}')
else:
    print('RTC (not found)')
assert _RTC_ADDRESS

# Is there a FRAM device (at 0x50)?
_FRAM_ADDRESS: Optional[int] = None
if 0x50 in _DEVICE_ADDRESSES:
    _FRAM_ADDRESS = 0x50
if _FRAM_ADDRESS:
    print(f'FRAM device={hex(_FRAM_ADDRESS)}')
else:
    print('FRAM (not found)')
assert _FRAM_ADDRESS

# Integer brightness limits (1..20).
# i.e. 1 (smallest) == 0.05 and 20 (largest) == 1.0
_MIN_BRIGHTNESS: int = 1
_MAX_BRIGHTNESS: int = 20

# Control button pin designation.
# We don't need a 'Pin.PULL_UP'
# because the buttons on the 'Pico Breadboard' are pulled down.
_BUTTON_1: Pin = Pin(11, Pin.IN)
_BUTTON_2: Pin = Pin(12, Pin.IN)
_BUTTON_3: Pin = Pin(13, Pin.IN)
_BUTTON_4: Pin = Pin(14, Pin.IN)

# A list of 2-letter month names
# Get name with simple lookup _MONTH_NAME[month_no]
# Get numerical value from string with _MONTH_NAME.index(month_str)
_MONTH_NAME: List[str] = ['xx',  # Unused (index = 0)
                          'Ja', 'Fe', 'Mc', 'Ap', 'Ma', 'Jn',
                          'Ju', 'Au', 'Se', 'Oc', 'No', 'De']
# Must have 13 entries (i.e. 12 plus dummy entry for month 0)
# and every value must contain 2 letters
assert len(_MONTH_NAME) == 13
for month_name in _MONTH_NAME:
    assert len(month_name) == 2
    assert month_name[0].isalpha()
    assert month_name[1].isalpha()

# A map of cumulative Days by month.
# Index is True for leap-year.
_CUMULATIVE_DAYS: Dict[bool, List[int]] = {
    False: [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334, 365],
    True: [0, 31, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335, 366]}

# Maximum days in a month (ignoring leap years)
# First entry (index 0) is invalid.
# Month index is 1-based (January = 1)
_DAYS: List[int] = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def leap_year(year: int) -> bool:
    """Returns True of the given year is a leap year.
    """
    return year % 4 == 0 and year % 100 != 0 or year % 400 == 0


def days_since_calibration(c_day: int, c_month: int,
                           now_day: int, now_month: int, now_year: int)\
        -> int:
    """Returns the number of days since calibration. If calibration day was
    yesterday '1' is returned. If it's the calibration day, '0' is returned.
    The maximum returned value is 365 (when we span a leap-year).
    At other times the maximum returned value is 364.
    """
    # Is it calibration day?
    if c_month == now_month and c_day == now_day:
        return 0

    # What's the offset today?
    # It's the day, plus the cumulative days
    # to the start of the current month, compensating for leap-year...
    now_offset: int = now_day +\
        _CUMULATIVE_DAYS[leap_year(now_year)][now_month - 1]

    # Does it look like now is before the calibration date?
    # If so we assume calibration was last year -
    # the calibration date cannot be n the future!
    if now_month < c_month or now_month == c_month and now_day < c_day:
        # Calibration year must have been last year.
        c_offset: int = c_day +\
            _CUMULATIVE_DAYS[leap_year(now_year - 1)][c_month - 1]
        # Now we know calibration was last year,
        # add all the days from last year to today
        # Cumulate days to end of december last year...
        now_offset += _CUMULATIVE_DAYS[leap_year(now_year - 1)][12]
    else:
        # Calibration is the current year
        c_offset = c_day
        if c_month > 1:
            c_offset += _CUMULATIVE_DAYS[leap_year(now_year)][c_month - 1]

    assert now_offset > c_offset
    return now_offset - c_offset


class RaRTC:
    """A wrapper around the Pimoroni BreakoutRTC class, a driver for
    the RV3028 RTC I2C breakout module.
    """

    def __init__(self):
        """Initialises the object.
        """
        # Create an object from the expected (built-in) Pimoroni library
        # that gives us access to the RV3028 RTC. We use this
        # to set the value after editing.
        self._pimoroni_i2c: PimoroniI2C = PimoroniI2C(sda=_SDA, scl=_SCL)
        self._rtc: BreakoutRTC = BreakoutRTC(self._pimoroni_i2c)
        # Setting backup switchover mode to '3'
        # means the device will switch to battery
        # when the power supply drops out.
        self._rtc.set_backup_switchover_mode(3)
        # And set to 24 hour mode (essential)
        self._rtc.set_24_hour()

    def datetime(self, new_datetime: Optional[RealTimeClock] = None)\
            -> RealTimeClock:
        """Gets (or sets and returns) the real-time clock.
        """
        if new_datetime is not None:
            # Given a date-time,
            # so use it to set the RTC value.
            #
            # We use a 1-based day of the week,
            # the underlying RTC uses 0-based.
            assert new_datetime.dow > 0
            self._rtc.set_time(new_datetime.s, new_datetime.m, new_datetime.h,
                               new_datetime.dow - 1,
                               new_datetime.dom,
                               new_datetime.month,
                               new_datetime.year)

        # Get the current RTC value,
        # waiting until a value is ready.
        new_rtc: Optional[RealTimeClock] = None
        while new_rtc is None:
            if self._rtc.update_time():
                new_rtc = RealTimeClock(
                    self._rtc.get_year(),
                    self._rtc.get_month(),
                    self._rtc.get_date(),
                    self._rtc.get_weekday() + 1,
                    self._rtc.get_hours(),
                    self._rtc.get_minutes(),
                    self._rtc.get_seconds())
            else:
                # No time available,
                # sleep for a very short period (less than a second)
                time.sleep_ms(250)  # type: ignore

        return new_rtc


class BaseFRAM:
    """Base driver for the FRAM breakout.
    """

    def __init__(self, i2c, address):
        """Create an instance for a devices at a given address.
        The device can reside at any of eight addresses, 0x50 - 0x57.
        """
        assert i2c
        assert address
        assert address >= 0x50
        assert address <= 0x57

        self._i2c = i2c
        self._address = address

    def write_byte(self, offset: int, byte_value: int) -> bool:
        """Writes a single value (expected to be a byte).
        Our implementation assumes the value is +ve (including zero),
        e.g. 0..127.
        """
        # Max offset is 32K
        assert offset >= 0
        assert offset < 32_768
        assert byte_value >= 0
        assert byte_value < 128

        num_ack = self._i2c.writeto(self._address,
                                    bytes([offset >> 8,
                                           offset & 0xff,
                                           byte_value]))
        if num_ack != 3:
            print(f'Failed to write to FRAM at {self._address}.' +
                  f' num_ack={num_ack}, expected 3')

        return num_ack != 3

    def read_byte(self, offset) -> int:
        """Reads a single byte, assumed to be in the range 0-127,
        returning it as an int.
        """
        # Max offset is 32K
        assert offset >= 0
        assert offset < 32_768

        num_acks = self._i2c.writeto(self._address,
                                     bytes([offset >> 8, offset & 0xff]))
        assert num_acks == 2
        got = self._i2c.readfrom(self._address, 1)
        assert got

        int_got: int = int.from_bytes(got, 'big')
        return int_got


class DisplayPair:
    """A simple class to control a LTP305 in MicroPython on a Pico. Based on
    Pimoroni's Raspberry Pi code at https://github.com/pimoroni/ltp305-python.
    Instead of using the Pi i2c library (which we can't use on the Pico)
    we use the MicroPython i2c library.

    The displays can use i2c address 0x61-0x63.
    """

    # LTP305 bitmaps for key characters indexed by character ordinal.
    # Digits, space and upper and lower-case letters.
    font: Dict[int, List[int]] = {
        32: [0x00, 0x00, 0x00, 0x00, 0x00],  # (space)

        #        48: [0x3e, 0x51, 0x49, 0x45, 0x3e],  # 0
        48: [0x3e, 0x41, 0x41, 0x41, 0x3e],  # O
        49: [0x00, 0x42, 0x7f, 0x40, 0x00],  # 1
        50: [0x42, 0x61, 0x51, 0x49, 0x46],  # 2
        51: [0x21, 0x41, 0x45, 0x4b, 0x31],  # 3
        52: [0x18, 0x14, 0x12, 0x7f, 0x10],  # 4
        53: [0x27, 0x45, 0x45, 0x45, 0x39],  # 5
        54: [0x3c, 0x4a, 0x49, 0x49, 0x30],  # 6
        55: [0x01, 0x71, 0x09, 0x05, 0x03],  # 7
        56: [0x36, 0x49, 0x49, 0x49, 0x36],  # 8
        57: [0x06, 0x49, 0x49, 0x29, 0x1e],  # 9

        65: [0x7e, 0x11, 0x11, 0x11, 0x7e],  # A
        66: [0x7f, 0x49, 0x49, 0x49, 0x36],  # B
        67: [0x3e, 0x41, 0x41, 0x41, 0x22],  # C
        68: [0x7f, 0x41, 0x41, 0x22, 0x1c],  # D
        69: [0x7f, 0x49, 0x49, 0x49, 0x41],  # E
        70: [0x7f, 0x09, 0x09, 0x01, 0x01],  # F
        71: [0x3e, 0x41, 0x41, 0x51, 0x32],  # G
        72: [0x7f, 0x08, 0x08, 0x08, 0x7f],  # H
        73: [0x00, 0x41, 0x7f, 0x41, 0x00],  # I
        74: [0x20, 0x40, 0x41, 0x3f, 0x01],  # J
        75: [0x7f, 0x08, 0x14, 0x22, 0x41],  # K
        76: [0x7f, 0x40, 0x40, 0x40, 0x40],  # L
        77: [0x7f, 0x02, 0x04, 0x02, 0x7f],  # M
        78: [0x7f, 0x04, 0x08, 0x10, 0x7f],  # N
        79: [0x3e, 0x41, 0x41, 0x41, 0x3e],  # O
        80: [0x7f, 0x09, 0x09, 0x09, 0x06],  # P
        81: [0x3e, 0x41, 0x51, 0x21, 0x5e],  # Q
        82: [0x7f, 0x09, 0x19, 0x29, 0x46],  # R
        83: [0x46, 0x49, 0x49, 0x49, 0x31],  # S
        84: [0x01, 0x01, 0x7f, 0x01, 0x01],  # T
        85: [0x3f, 0x40, 0x40, 0x40, 0x3f],  # U
        86: [0x1f, 0x20, 0x40, 0x20, 0x1f],  # V
        87: [0x7f, 0x20, 0x18, 0x20, 0x7f],  # W
        88: [0x63, 0x14, 0x08, 0x14, 0x63],  # X
        89: [0x03, 0x04, 0x78, 0x04, 0x03],  # Y
        90: [0x61, 0x51, 0x49, 0x45, 0x43],  # Z

        97: [0x20, 0x54, 0x54, 0x54, 0x78],  # a
        98: [0x7f, 0x48, 0x44, 0x44, 0x38],  # b
        99: [0x38, 0x44, 0x44, 0x44, 0x20],  # c
        100: [0x38, 0x44, 0x44, 0x48, 0x7f],  # d
        101: [0x38, 0x54, 0x54, 0x54, 0x18],  # e
        102: [0x08, 0x7e, 0x09, 0x01, 0x02],  # f
        103: [0x08, 0x14, 0x54, 0x54, 0x3c],  # g
        104: [0x7f, 0x08, 0x04, 0x04, 0x78],  # h
        105: [0x00, 0x44, 0x7d, 0x40, 0x00],  # i
        106: [0x20, 0x40, 0x44, 0x3d, 0x00],  # j
        107: [0x00, 0x7f, 0x10, 0x28, 0x44],  # k
        108: [0x00, 0x41, 0x7f, 0x40, 0x00],  # l
        109: [0x7c, 0x04, 0x18, 0x04, 0x78],  # m
        110: [0x7c, 0x08, 0x04, 0x04, 0x78],  # n
        111: [0x38, 0x44, 0x44, 0x44, 0x38],  # o
        112: [0x7c, 0x14, 0x14, 0x14, 0x08],  # p
        113: [0x08, 0x14, 0x14, 0x18, 0x7c],  # q
        114: [0x7c, 0x08, 0x04, 0x04, 0x08],  # r
        115: [0x48, 0x54, 0x54, 0x54, 0x20],  # s
        116: [0x04, 0x3f, 0x44, 0x40, 0x20],  # t
        117: [0x3c, 0x40, 0x40, 0x20, 0x7c],  # u
        118: [0x1c, 0x20, 0x40, 0x20, 0x1c],  # v
        119: [0x3c, 0x40, 0x30, 0x40, 0x3c],  # w
        120: [0x44, 0x28, 0x10, 0x28, 0x44],  # x
        121: [0x0c, 0x50, 0x50, 0x50, 0x3c],  # y
        122: [0x44, 0x64, 0x54, 0x4c, 0x44],  # z
    }

    MODE = 0b00011000
    OPTS = 0b00001110  # 1110 = 35mA, 0000 = 40mA
    UPDATE = 0x01

    CMD_BRIGHTNESS = 0x19
    CMD_MODE = 0x00
    CMD_UPDATE = 0x0C
    CMD_OPTIONS = 0x0D

    CMD_MATRIX_L = 0x0E
    CMD_MATRIX_R = 0x01

    def __init__(self, i2c, address: int = 0x61, brightness: float = 0.1):
        assert i2c
        assert address in [0x61, 0x62, 0x63]

        self._bus = i2c
        self._address: int = address
        self._buf_matrix_left: List[int] = []
        self._buf_matrix_right: List[int] = []
        self._brightness: int = 0

        self.set_brightness(brightness)
        self.clear()

    def clear(self) -> None:
        """Clear both LED matrices.

        Must call .show() to display changes.
        """
        self._buf_matrix_left = [0 for _ in range(8)]
        self._buf_matrix_right = [0 for _ in range(8)]

    def set_brightness(self, brightness: float, update: bool = False) -> None:
        """Set brightness of both LED matrices (from 0.0 to 1.0).
        """
        assert brightness >= 0.0
        assert brightness <= 1.0

        _brightness = int(brightness * 127.0)
        self._brightness = min(127, max(0, _brightness))
        if update:
            self._bus.writeto_mem(self._address,
                                  DisplayPair.CMD_BRIGHTNESS,
                                  self._brightness.to_bytes(1, 'big'))

    def set_pixel(self, px: int, py: int, val: int) -> None:
        """Set a single pixel on the matrix.
        """
        if px < 5:  # Left Matrix
            if val:
                self._buf_matrix_left[px] |= (0b1 << py)
            else:
                self._buf_matrix_left[px] &= ~(0b1 << py)
        else:  # Right Matrix
            px -= 5
            if val:
                self._buf_matrix_right[py] |= (0b1 << px)
            else:
                self._buf_matrix_right[py] &= ~(0b1 << px)

    def set_pair(self, chars: str) -> None:
        """Set a character pair.
        """
        assert isinstance(chars, str)
        assert len(chars) == 2

        self.set_character(0, chars[0])
        self.set_character(5, chars[1])

    def set_character(self, x: int, char: Union[int, str]) -> None:
        """Set a single character.
        """
        if not isinstance(char, int):
            assert isinstance(char, str)
            char = ord(char)
        pixel_data: List[int] = DisplayPair.font[char]
        for px in range(5):
            for py in range(8):
                c = pixel_data[px] & (0b1 << py)
                self.set_pixel(x + px, py, c)

    def show(self) -> None:
        """Update the LED matrix from the buffer.
        """
        self._bus.writeto_mem(self._address,
                              DisplayPair.CMD_MATRIX_L,
                              bytearray(self._buf_matrix_left))
        self._bus.writeto_mem(self._address,
                              DisplayPair.CMD_MATRIX_R,
                              bytearray(self._buf_matrix_right))
        self._bus.writeto_mem(self._address,
                              DisplayPair.CMD_MODE,
                              DisplayPair.MODE.to_bytes(1, 'big'))
        self._bus.writeto_mem(self._address,
                              DisplayPair.CMD_OPTIONS,
                              DisplayPair.OPTS.to_bytes(1, 'big'))
        self._bus.writeto_mem(self._address,
                              DisplayPair.CMD_BRIGHTNESS,
                              self._brightness.to_bytes(1, 'big'))
        self._bus.writeto_mem(self._address,
                              DisplayPair.CMD_UPDATE,
                              DisplayPair.UPDATE.to_bytes(1, 'big'))


class RaDisplay:
    """A wrapper around two LTP305 objects to form a 4-character display.
    Basically a 4-character display used to display the compensated RA value,
    target RA, the current time, and the calibration date.
    """

    def __init__(self, i2c, rtc: RaRTC, address_l: int, address_r: int):
        """Initialises the display pair object. Given an i2c instance,
        an RaRTC object, left and right display addresses,
        and an optional brightness.
        """
        assert i2c
        assert rtc

        self._rtc = rtc
        self._brightness_f: float = _MIN_BRIGHTNESS / _MAX_BRIGHTNESS

        self.l_matrix = DisplayPair(i2c,
                                    address=address_l,
                                    brightness=self._brightness_f)
        self.r_matrix = DisplayPair(i2c,
                                    address=address_r,
                                    brightness=self._brightness_f)

    def set_brightness(self, brightness: int) -> None:
        assert brightness >= _MIN_BRIGHTNESS
        assert brightness <= _MAX_BRIGHTNESS

        # Remember this setting
        self._brightness_f = brightness / _MAX_BRIGHTNESS

        self.l_matrix.set_brightness(self._brightness_f, True)
        self.r_matrix.set_brightness(self._brightness_f, True)

    def clear(self, left: bool = True, right: bool = True) -> None:
        if left:
            self.l_matrix.clear()
            self.l_matrix.show()

        if right:
            self.r_matrix.clear()
            self.r_matrix.show()

    def show_ra(self, ra_target: RA, calibration_date: CalibrationDate) \
            -> None:
        """Uses the RTC to calculate the corrected RA value for the
        given RA target value and its calibration date (with defaults).
        """
        assert ra_target
        assert calibration_date

        # Read the RTC
        # We're given an 8-value tuple with the following content:
        # (year, month, day, weekday, hours, minutes, seconds, sub-seconds)
        rtc = self._rtc.datetime()
        # We just need 'HH:MM', which we'll call 'clock'.
        clock: str = f'{rtc[4]:02d}:{rtc[5]:02d}'

        # Calculate the corrected RA.
        #
        # First, We add the current time to the target RA.
        target_ra_minutes: int = ra_target.h * 60 + ra_target.m
        clock_hours: int = int(clock[:2])
        clock_minutes: int = clock_hours * 60 + int(clock[3:])
        scope_ra_minutes: int = target_ra_minutes + clock_minutes
        # Then, we add 1 minute for every 6 hours on the clock.
        # i.e. after every 6 hours the celestial bodies will move by 1 minute.
        # This accommodates the sky's progression for the current day.
        sub_day_offset: int = clock_hours // 6
        scope_ra_minutes += sub_day_offset
        # Then, add 4 minutes for each whole day since calibration.
        # The RTC date format is 'dd/mm/yyyy'.
        # The celestial bodies drift by 4 minutes per day
        # The maximum correction is 364 days. After each year we're back to
        # a daily offset of '0'.
        date_day: int = rtc[2]
        date_month: int = rtc[1]
        date_year: int = rtc[0]
        elapsed_days: int = days_since_calibration(calibration_date.d,
                                                   calibration_date.m,
                                                   date_day,
                                                   date_month,
                                                   date_year)
        assert elapsed_days >= 0
        # If calibration was on the 4th and today is the 5th the days between
        # the dates is '1' but, the first 24 hours is handled by the
        # 'sub_day_offset' so we must only count whole days, i.e. we subtract
        # '1' from the result to accommodate the
        # 'sub_day_offset'.
        whole_days: int = elapsed_days - 1 if elapsed_days else 0
        whole_days_offset: int = 4 * whole_days
        scope_ra_minutes += whole_days_offset
        # Finally, if the resultant minutes amounts to more than 24 hours
        # then wrap the time, i.e. 24:01 becomes 0:01.
        if scope_ra_minutes >= _DAY_MINUTES:
            scope_ra_minutes -= _DAY_MINUTES
        # Convert minutes to hours and minutes,
        # which gives us our corrected RA axis value.
        scope_ra_hours: int = scope_ra_minutes // 60
        scope_ra_minutes = scope_ra_minutes % 60
        scope_ra_human: str = f'{scope_ra_hours}h{scope_ra_minutes:02d}m'
        print(f'For RA {ra_target.h}h{ra_target.m}m' +
              f' set RA Axis to {scope_ra_human}' +
              f' @ {clock}' +
              f' + {whole_days_offset}m ({whole_days} days)' +
              f' + {sub_day_offset}m ({clock_hours:02d}:**)')
        # Display
        scope_ra: str = f'{scope_ra_hours:02d}{scope_ra_minutes:02d}'
        self.show(scope_ra)

    def show_time(self,
                  hour: Optional[int] = None,
                  minute: Optional[int] = None) -> None:
        """Displays the current RTC unless an hour and minute are specified.
        """

        if not hour or not minute:
            rtc_time: Tuple = self._rtc.datetime()
            # We just need HHMM, which we'll call 'clock'.
            clock: str = f'{rtc_time[4]:02d}{rtc_time[5]:02d}'
        else:
            # User-provided value
            clock = f'{hour:02d}{minute:02d}'

        # Display
        assert len(clock) == 4
        self.show(clock)

    def show_ra_target(self, ra_target) -> None:
        """Displays the raw RA target value.
        """
        # Just display the raw RA value
        clock: str = f'{ra_target.h:02d}{ra_target.m:02d}'

        # Display
        self.show(clock)

    def show_calibration_date(self, calibration_date) -> None:
        """Displays the current calibration_date (day and month).
        The month is rendered as a two-letter abbreviation to avoid
        confusion with the target RA.
        """
        # The left-hand value (numerical day)
        clock: str = f'{calibration_date.d:2d}'
        # The right-hand value (abbreviated month)
        clock += _MONTH_NAME[calibration_date.m]

        # Display
        self.show(clock)

    def show(self, value: str) -> None:
        """Set the display, given a 4-digit string '[HH][MM]'.
        """
        assert isinstance(value, str)
        assert len(value) == 4

        # Hour [HH] (leading zero replaced by ' ')
        if value[0] == '0':
            self.l_matrix.set_pair(' ' + value[1:2])
        else:
            self.l_matrix.set_pair(value[:2])
        # Minute [MM]
        self.r_matrix.set_pair(value[2:])

        self.l_matrix.show()
        self.r_matrix.show()


class RaFRAM:
    """A RA wrapper around a FRAM class. This class provides convenient
    RA-specific storage using the underlying FRAM. Here we provide
    methods to simplify the storage and retrieval of 'brightness',
    'RA target' and 'calibration date', all persisted safely in a FRAM
    module.

    Changes to values are written and cached minimising the number
    of FRAM reads that take place.
    """

    # Default values,
    # used when reading if no FRAM value exists.

    # Default brightness (lowest)
    DEFAULT_BRIGHTNESS: int = _MIN_BRIGHTNESS

    # Markers.
    # Values that prefix every stored value. These are used to indicate
    # whether the corresponding (potentially multibyte) value
    # is either valid or invalid.
    # Markers (and data values) must be +ve byte values (0-127)
    _INVALID: int = 0  # The value cannot be trusted
    _VALID: int = 33  # The value can be trusted

    # Memory Map
    #
    # +--------+----------------------------------
    # | Offset | Purpose
    # +--------+----------------------------------
    # | *   0  | Brightness Marker
    # |     1  | Brightness Value [1..20]
    # | *   2  | RA Target Marker
    # |     3  | RA Target (Hours) [0..23]
    # |     4  | RA Target (Minutes) [0..59]
    # | *   5  | Calibration Date Marker
    # |     6  | Calibration Date (Day) [1..31]
    # |     7  | Calibration Date (Month) [1..12]
    # +--------+----------------------------------
    _OFFSET_BRIGHTNESS: int = 0  # 1 byte
    _OFFSET_RA_TARGET: int = 2  # 2 bytes
    _OFFSET_CALIBRATION_DATE: int = 5  # 2 bytes

    def __init__(self, fram: BaseFRAM):
        assert fram

        # Save the FRAM reference
        self._fram: BaseFRAM = fram

        # Cached values of data.
        # Set when reading or writing the corresponding values.
        self._ra_target: Optional[RA] = None
        self._brightness: Optional[int] = None
        self._calibration_date: Optional[CalibrationDate] = None

    def _write_value(self, offset: int, value: Union[int, List[int]]) -> None:
        assert offset >= 0

        # Set marker to invalid
        # Write value (or values)
        # Set marker to valid
        self._fram.write_byte(offset, RaFRAM._INVALID)
        if isinstance(value, int):
            assert value >= 0
            assert value <= 127
            self._fram.write_byte(offset + 1, value)
        else:
            assert isinstance(value, list)
            value_offset: int = offset + 1
            for a_value in value:
                assert a_value >= 0
                assert a_value <= 127
                self._fram.write_byte(value_offset, a_value)
                value_offset += 1
        self._fram.write_byte(offset, RaFRAM._VALID)

    def _read_value(self, offset: int) -> int:
        """Reads the value form the offset.
        The offset provided is the marker, we read the value
        after the marker.
        """
        assert offset >= 0
        assert offset + 1 < 32_768

        value: int = self._fram.read_byte(offset + 1)
        assert value >= 0

        return value

    def _read_values(self, offset: int, length: int) -> List[int]:
        """Reads the values form the offset.
        The offset provided is the marker, we start reading
        after the marker.
        """
        assert offset >= 0
        assert length > 0
        assert offset + 1 + length < 32_768

        value: List[int] = []
        for value_offset in range(length):
            value.append(self._fram.read_byte(offset + 1 + value_offset))
        assert len(value) == length

        return value

    def _is_value_valid(self, offset: int) -> bool:
        assert offset >= 0

        byte_value: int = self._fram.read_byte(offset)
        value: bool = byte_value == RaFRAM._VALID

        return value

    def read_brightness(self) -> int:

        # Return the cached (last written) value if we have it
        if self._brightness:
            return self._brightness
        # Is there a value in the FRAM?
        # If so, read it, put it in the cache and return it.
        if self._is_value_valid(RaFRAM._OFFSET_BRIGHTNESS):
            self._brightness = self._read_value(RaFRAM._OFFSET_BRIGHTNESS)
            return self._brightness
        # No cached value, no stored value,
        # so write and then return the default
        self.write_brightness(RaFRAM.DEFAULT_BRIGHTNESS)
        assert self._brightness
        return self._brightness

    def write_brightness(self, brightness: int) -> None:
        assert brightness >= _MIN_BRIGHTNESS
        assert brightness <= _MAX_BRIGHTNESS

        # Write to FRAM
        self._write_value(RaFRAM._OFFSET_BRIGHTNESS, brightness)

        # Finally, save the value to the cached value
        self._brightness = brightness

    def read_ra_target(self) -> RA:

        # Return the cached (last written) value if we have it
        if self._ra_target:
            return self._ra_target
        # Is there a value in the FRAM?
        # If so, read it, put it in the cache and return it.
        if self._is_value_valid(RaFRAM._OFFSET_RA_TARGET):
            value: List[int] = self._read_values(RaFRAM._OFFSET_RA_TARGET, 2)
            self._ra_target = RA(value[0], value[1])
            return self._ra_target
        # No cached value,
        # so write and then return the default
        self.write_ra_target(DEFAULT_RA_TARGET)
        return self._ra_target

    def write_ra_target(self, ra_target: RA) -> None:
        assert ra_target

        # Write to FRAM
        values: List[int] = [ra_target.h, ra_target.m]
        self._write_value(RaFRAM._OFFSET_RA_TARGET, values)

        # Finally, save the value to the cached value
        self._ra_target = ra_target

    def read_calibration_date(self) -> CalibrationDate:

        # Return the cached (last written) value if we have it
        if self._calibration_date:
            return self._calibration_date
        # Is there a value in the FRAM?
        # If so, read it, put it in the cache and return it.
        if self._is_value_valid(RaFRAM._OFFSET_CALIBRATION_DATE):
            value: List[int] = \
                self._read_values(RaFRAM._OFFSET_CALIBRATION_DATE, 2)
            self._calibration_date = CalibrationDate(value[0], value[1])
            return self._calibration_date
        # No cached value,
        # so write and then return the default
        self.write_calibration_date(DEFAULT_CALIBRATION_DATE)
        return self._calibration_date

    def write_calibration_date(self, calibration_date: CalibrationDate) \
            -> None:
        assert calibration_date

        # Write to FRAN
        values: List[int] = [calibration_date.d, calibration_date.m]
        self._write_value(RaFRAM._OFFSET_CALIBRATION_DATE, values)

        # Finally, save the value to the cached value
        self._calibration_date = calibration_date

    def clear(self):
        """Clears, invalidates, the RA FRAM values.
        """
        self._fram.write_byte(RaFRAM._OFFSET_BRIGHTNESS, RaFRAM._INVALID)
        self._fram.write_byte(RaFRAM._OFFSET_RA_TARGET, RaFRAM._INVALID)
        self._fram.write_byte(RaFRAM._OFFSET_CALIBRATION_DATE, RaFRAM._INVALID)


# The command queue - the object between the
# buttons, timers and the main-loop state machine.
# At the moment we only handle one command at a time,
# i.e. queue size is 1.
class CommandQueue:

    def __init__(self):
        self._queue_size: int = 1
        self._queue = []
    
    def members(self) -> int:
        return len(self._queue)

    def clear(self) -> None:
        self._queue.clear()

    def put(self, command: int) -> None:
        if len(self._queue) < self._queue_size:
            self._queue.append(command)
    
    def get(self) -> Optional[int]:
        if self.members():
            return self._queue.pop(0)
        return None


# CommandQueue commands (just unique integers)
_CMD_BUTTON_1: int = 1          # Button 1 has been pressed
_CMD_BUTTON_2: int = 2          # Button 2 has been pressed
_CMD_BUTTON_2_LONG: int = 22    # Button 2 has been pressed for a long time
_CMD_BUTTON_3: int = 3          # Button 3 has been pressed
_CMD_BUTTON_4: int = 4          # Button 4 has been pressed
_CMD_BUTTON_4_LONG: int = 44    # Button 4 has been pressed for a long time
_CMD_TICK: int = 10             # The timer has fired


def btn_1(pin: Pin) -> None:
    """The '**DISPLAY** button. Creates the _CMD_BUTTON_1 command.

    Pressing this when the display is off
    will display the current (real-time) RA axis compensation value.
    Pressing it when the display is on cycles between other values
    (like the RA target, current time and calibration date).
    """

    # Crude debounce.
    # We disable theis pin's interrupt and
    # pause here for a debounce period. If the pin is still pressed
    # after we wake up then we can safely react to the button.
    pin.irq(handler=None)
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    if pin.value():
        _COMMAND_QUEUE.put(_CMD_BUTTON_1)
    pin.irq(trigger=Pin.IRQ_RISING, handler=btn_1)


def btn_2(pin: Pin) -> None:
    """The **PROGRAM** button. Creates the _CMD_BUTTON_2 and _CMD_BUTTON_2_LONG
    commands.

    Pressing this on a programmable value is displayed (like the target RA)
    enters the programming mode for the displayed value. IN programming mode
    thr UP/DOWN buttons are used to alter the displayed value.

    The programming value is committed by holding this button for a few seconds
    (_LONG_BUTTON_PRESS_MS). Programing mode is cancelled by hitting the
    **DISPLAY** button.
    """

    pin.irq(handler=None)
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    # Measure the time pressed.
    # Short, we insert a _CMD_BUTTON_2 command,
    # Long, we insert a _CMD_BUTTON_2_LONG command.
    if pin.value():
        down_ms: int = time.ticks_ms()  # type: ignore
        depressed: bool = True
        while depressed:
            time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
            if not pin.value():
                depressed = False
        up_ms: int = time.ticks_ms()  # type: ignore
        duration: int = time.ticks_diff(up_ms, down_ms)  # type: ignore
        if duration >= _LONG_BUTTON_PRESS_MS:
            _COMMAND_QUEUE.put(_CMD_BUTTON_2_LONG)
        else:
            _COMMAND_QUEUE.put(_CMD_BUTTON_2)
    pin.irq(trigger=Pin.IRQ_RISING, handler=btn_2)


def btn_3(pin: Pin) -> None:
    """The **DOWN** button. Creates the _CMD_BUTTON_3 command.

    Pressing this when the display is on decreases
    the display brightness. In programming mode it decreases the value that
    is flashing.
    """

    pin.irq(handler=None)
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    if pin.value():
        _COMMAND_QUEUE.put(_CMD_BUTTON_3)
    pin.irq(trigger=Pin.IRQ_RISING, handler=btn_3)


def btn_4(pin: Pin) -> None:
    """The **UP** button. Creates the _CMD_BUTTON_4 and _CMD_BUTTON_4_LONG
    commands

    Pressing this when the display is on increases
    the display brightness. In programming mode it increases the value that
    is flashing.

    A long press (programming or not) will result in a trigger
    for the state machine toi exit.
    """

    pin.irq(handler=None)
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    # Measure the time pressed.
    # Short, we insert a _CMD_BUTTON_4 command,
    # Long, we insert a _CMD_BUTTON_4_LONG command.
    if pin.value():
        down_ms: int = time.ticks_ms()  # type: ignore
        depressed: bool = True
        while depressed:
            time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
            if not pin.value():
                depressed = False
        up_ms: int = time.ticks_ms()  # type: ignore
        duration: int = time.ticks_diff(up_ms, down_ms)  # type: ignore
        if duration >= _LONG_BUTTON_PRESS_MS:
            _COMMAND_QUEUE.put(_CMD_BUTTON_4_LONG)
        else:
            _COMMAND_QUEUE.put(_CMD_BUTTON_4)
    pin.irq(trigger=Pin.IRQ_RISING, handler=btn_4)


def tick(timer):
    """A timer callback. Creates the _CMD_TICK command.

    Enabled only when display is on.
    """
    assert timer

    _COMMAND_QUEUE.put(_CMD_TICK)


class StateMachine:
    """The application state machine.
    """
    # The individual states
    S_IDLE: int = 0
    S_DISPLAY_RA: int = 1
    S_DISPLAY_RA_TARGET: int = 2
    S_DISPLAY_CLOCK: int = 3
    S_DISPLAY_C_DATE: int = 4
    S_PROGRAM_RA_TARGET_H: int = 5
    S_PROGRAM_RA_TARGET_M: int = 6
    S_PROGRAM_CLOCK: int = 7
    S_PROGRAM_C_DAY: int = 8
    S_PROGRAM_C_MONTH: int = 9

    # Timer period (milliseconds).
    # When it's enabled a _CMD_TICK command is issued at this rate.
    TIMER_PERIOD_MS: int = 500
    # Number of timer ticks to hold the display before returning to idle
    # (8 is 4 seconds when the timer is 500mS)
    HOLD_TICKS: int = 8

    def __init__(self, display: RaDisplay, ra_fram: RaFRAM, rtc: RaRTC):
        assert display
        assert ra_fram
        assert rtc

        # The current state
        self._state: int = StateMachine.S_IDLE
        # A countdown timer.
        # Each _CMD_TICK command decrements this value until it reaches zero.
        # When it reaches zero the display is cleared (state returns to IDLE).
        self._to_idle_countdown: int = 0
        # The display, FRAM and RTC
        self._display: RaDisplay = display
        self._ra_fram: RaFRAM = ra_fram
        self._rtc: RaRTC = rtc
        # Read brightness, RA target and calibration date from
        # the FRAM. Defaults will be used if written values are not found.
        self._brightness: int = self._ra_fram.read_brightness()
        self._ra_target: RA = self._ra_fram.read_ra_target()
        self._calibration_date: CalibrationDate = \
            self._ra_fram.read_calibration_date()

        # Set the display's initial brightness
        self._display.set_brightness(self._brightness)

        # A Timer object.
        # Initialised when something is displayed.
        # De-initialised when the display is cleared.
        # The timer runs outside the context of this object,
        # we just enable and disable it.
        self._timer: Optional[Timer] = None

        # Program mode variables.
        # When in program mode the display flashes.
        # Whether the left, right or left and right displays flash
        # will depend on the state we're in.
        self._programming: bool = False
        self._programming_left: bool = False
        self._programming_right: bool = False
        # The state that's being programmed.
        self._programming_state: int = StateMachine.S_IDLE
        # Current visibility of left and right digit-pair
        # These values toggled and use to flash the appropriate character pair
        # on each timer tick depending on the value of
        # _programming_left or _programming_right.
        self._programming_left_on: bool = False
        self._programming_right_on: bool = False
        # The 4-character string value to present to the display
        # when programming.
        self._programming_value: Optional[str] = None

    def _clear_program_mode(self) -> None:
        self._programming = False
        self._programming_value = None

    def _start_timer(self, to_idle: bool = True) -> None:
        """Starts the timer.
        If to_idle is True the timer is a countdown to idle.
        where the mode will rever to idle when the countdown is complete.
        """
        if self._timer is None:
            self._timer = Timer(period=StateMachine.TIMER_PERIOD_MS,
                                callback=tick)
        if to_idle:
            self._to_idle_countdown = StateMachine.HOLD_TICKS

    def _stop_timer(self) -> None:
        if self._timer:
            self._timer.deinit()
            self._timer = None
        self._to_idle_countdown = 0

    def _program_up(self) -> None:
        """Called when the 'UP' button has been pressed in program mode.
        Here we need to appropriately increment the _programming_value.
        """
        assert self._programming_value

        if self._state == StateMachine.S_PROGRAM_CLOCK:
            # Run the clock backwards
            hour: int = int(self._programming_value[:2])
            minute: int = int(self._programming_value[2:])
            minute += 1
            if minute > 59:
                minute = 0
                hour += 1
                if hour > 23:
                    hour = 0
            self._programming_value = f'{hour:02d}{minute:02d}'
        elif self._state == StateMachine.S_PROGRAM_RA_TARGET_H:
            # Adjust the hours only
            hour = int(self._programming_value[:2])
            minute = int(self._programming_value[2:])
            hour += 1
            if hour > 23:
                hour = 0
            self._programming_value = f'{hour:02d}{minute:02d}'
        elif self._state == StateMachine.S_PROGRAM_RA_TARGET_M:
            # Adjust the minutes only
            hour = int(self._programming_value[:2])
            minute = int(self._programming_value[2:])
            minute += 1
            if minute > 59:
                minute = 0
            self._programming_value = f'{hour:02d}{minute:02d}'
        elif self._state == StateMachine.S_PROGRAM_RA_TARGET_M:
            # Adjust the minutes only
            hour = int(self._programming_value[:2])
            minute = int(self._programming_value[2:])
            minute += 1
            if minute > 59:
                minute = 0
            self._programming_value = f'{hour:02d}{minute:02d}'
        elif self._state == StateMachine.S_PROGRAM_C_MONTH:
            # Adjust the month only
            day: int = int(self._programming_value[:2])
            month: int = _MONTH_NAME.index(self._programming_value[2:])
            assert month >= 1
            month += 1
            if month > 12:
                month = 1
            # If the day number is now too large for this month,
            # set it to the maximum for the current month.
            day = min(_DAYS[month], day)
            self._programming_value = f'{day:2d}{_MONTH_NAME[month]}'
        elif self._state == StateMachine.S_PROGRAM_C_DAY:
            # Adjust the day only
            day = int(self._programming_value[:2])
            month = _MONTH_NAME.index(self._programming_value[2:])
            day += 1
            if day > _DAYS[month]:
                day = 1
            self._programming_value = f'{day:2d}{_MONTH_NAME[month]}'

    def _program_down(self) -> None:
        """Called when the 'DOWN' button has been pressed in program mode.
        Here we need to appropriately decrement the _programming_value.
        """
        assert self._programming_value

        if self._state == StateMachine.S_PROGRAM_CLOCK:
            # Run the clock backwards
            hour: int = int(self._programming_value[:2])
            minute: int = int(self._programming_value[2:])
            minute -= 1
            if minute < 0:
                minute = 59
                hour -= 1
                if hour < 0:
                    hour = 23
            self._programming_value = f'{hour:02d}{minute:02d}'
        elif self._state == StateMachine.S_PROGRAM_RA_TARGET_H:
            # Adjust the hours only
            hour = int(self._programming_value[:2])
            minute = int(self._programming_value[2:])
            hour -= 1
            if hour < 0:
                hour = 23
            self._programming_value = f'{hour:2d}{minute:02d}'
        elif self._state == StateMachine.S_PROGRAM_RA_TARGET_M:
            # Adjust the minutes only
            hour = int(self._programming_value[:2])
            minute = int(self._programming_value[2:])
            minute -= 1
            if minute < 0:
                minute = 59
            self._programming_value = f'{hour:02d}{minute:02d}'
        elif self._state == StateMachine.S_PROGRAM_C_MONTH:
            # Adjust the month only
            day: int = int(self._programming_value[:2])
            month: int = _MONTH_NAME.index(self._programming_value[2:])
            assert month >= 1
            month -= 1
            if month < 1:
                month = 12
            # If the day number is now too large for this month,
            # set it to the maximum for the current month.
            day = min(_DAYS[month], day)
            self._programming_value = f'{day:2d}{_MONTH_NAME[month]}'
        elif self._state == StateMachine.S_PROGRAM_C_DAY:
            # Adjust the day only
            day = int(self._programming_value[:2])
            month = _MONTH_NAME.index(self._programming_value[2:])
            day -= 1
            if day < 1:
                day = _DAYS[month]
            self._programming_value = f'{day:2d}{_MONTH_NAME[month]}'

    def process_command(self, command: int) -> bool:
        """Process a command, where the actions depend on the
        current 'state'.
        """

        if command == _CMD_BUTTON_4_LONG:
            # A 'kill' command,
            # Returning False means the app will stop.
            return False

        if command == _CMD_TICK:
            # Internal TICK (500mS)

            if self._state in [StateMachine.S_IDLE]:
                # Nothing to do
                return True

            # Auto-to-idle countdown?
            if self._to_idle_countdown:
                # If we get a TICK, all we do is decrement
                # the countdown timer to 0. When it reaches
                # zero we move to the IDLE state.
                self._to_idle_countdown -= 1
                if self._to_idle_countdown == 0:
                    return self._to_idle()
                return True

            # Programming mode?
            if self._programming:
                assert self._programming_value
                self._display.show(self._programming_value)
                # Toggle left and right visibility depending on
                # whether we're programming left or right or both.
                if self._programming_left:
                    if self._programming_left_on:
                        self._display.clear(right=False)
                        self._programming_left_on = False
                    else:
                        self._programming_left_on = True
                if self._programming_right:
                    if self._programming_right_on:
                        self._display.clear(left=False)
                        self._programming_right_on = False
                    else:
                        self._programming_right_on = True

            # Handled if we get here
            return True

        if command == _CMD_BUTTON_1:
            # "DISPLAY" button

            # If not in programming mode we switch to another item to display.
            # Here we cancel programming mode if it's set
            # and then enter the normal mode of the item being displayed.

            # Non-programming states
            if self._state == StateMachine.S_IDLE:
                return self._to_display_ra()
            if self._state == StateMachine.S_DISPLAY_RA:
                return self._to_display_ra_target()
            if self._state == StateMachine.S_DISPLAY_RA_TARGET:
                return self._to_display_clock()
            if self._state == StateMachine.S_DISPLAY_CLOCK:
                return self._to_display_calibration_date()
            if self._state == StateMachine.S_DISPLAY_C_DATE:
                return self._to_display_ra()

            # Programming states
            if self._programming:
                # Switch programming off.
                # Return to the state that was being programmed...
                self._programming = False
                if self._programming_state == StateMachine.S_DISPLAY_RA_TARGET:
                    return self._to_display_ra_target()
                if self._programming_state == StateMachine.S_DISPLAY_CLOCK:
                    return self._to_display_clock()
                if self._programming_state == StateMachine.S_DISPLAY_C_DATE:
                    return self._to_display_calibration_date()

            # If all else fails, nothing to do
            return True

        if command == _CMD_BUTTON_2:
            # "PROGRAM" button

            # Into programming mode (from valid non-programming states)
            if self._state == StateMachine.S_DISPLAY_RA_TARGET:
                return self._to_program_ra_target_h()
            if self._state == StateMachine.S_DISPLAY_CLOCK:
                return self._to_program_clock()
            if self._state == StateMachine.S_DISPLAY_C_DATE:
                return self._to_program_calibration_day()

                # Move from left to right editing
            # Applies when editing the RA Target or Calibration Date
            if self._state == StateMachine.S_PROGRAM_RA_TARGET_H:
                return self._to_program_ra_target_m()
            if self._state == StateMachine.S_PROGRAM_RA_TARGET_M:
                return self._to_program_ra_target_h()

            if self._state == StateMachine.S_PROGRAM_C_DAY:
                return self._to_program_calibration_month()
            if self._state == StateMachine.S_PROGRAM_C_MONTH:
                return self._to_program_calibration_day()

                # If we get here, nothing to do
            return True

        if command == _CMD_BUTTON_2_LONG:
            # The program button's been pressed for a long time.
            # This should be used to 'save' any programming value.

            # Only act if we're in 'programming' mode.
            if self._programming:
                assert self._programming_value
                if self._state in [StateMachine.S_PROGRAM_RA_TARGET_H,
                                   StateMachine.S_PROGRAM_RA_TARGET_M]:
                    # The Target RA was being edited
                    ra_h: int = int(self._programming_value[:2])
                    ra_m: int = int(self._programming_value[2:])
                    self._ra_target = RA(ra_h, ra_m)
                    self._ra_fram.write_ra_target(self._ra_target)
                    # With the RA target changed, the best state to
                    # return to is to display the new corrected RA
                    return self._to_display_ra()

                if self._state in [StateMachine.S_PROGRAM_CLOCK]:
                    # The clock was being edited
                    hour: int = int(self._programming_value[:2])
                    minute: int = int(self._programming_value[2:])
                    # Read the current time and write the new
                    # hours and minutes. We're given an 8-value tuple
                    # with the following content:
                    # (y, m, d, weekday, h, m, seconds, sub-seconds)
                    rtc: RealTimeClock = self._rtc.datetime()
                    # Rest the seconds
                    rtc.h = hour
                    rtc.m = minute
                    rtc.s = 0
                    rtc_result = self._rtc.datetime(rtc)
                    # And then move to displaying the clock
                    return self._to_display_clock()

                if self._state in [StateMachine.S_PROGRAM_C_MONTH,
                                   StateMachine.S_PROGRAM_C_DAY]:
                    # The calibration date was being edited
                    day: int = int(self._programming_value[:2])
                    month: int = _MONTH_NAME.index(self._programming_value[2:])
                    assert month >= 1
                    self._calibration_date = CalibrationDate(day, month)
                    self._ra_fram \
                        .write_calibration_date(self._calibration_date)
                    # With the calibration date changed, the best state to
                    # return to is to display the new corrected date
                    return self._to_display_calibration_date()

            # Nothing to do yet
            return True

        if command == _CMD_BUTTON_3:
            # "DOWN" button

            if self._state not in [StateMachine.S_IDLE]:
                if self._programming:
                    self._program_down()
                else:
                    # Decrease display brightness,
                    # and reset the timer.
                    self._to_idle_countdown = StateMachine.HOLD_TICKS
                    if self._brightness > _MIN_BRIGHTNESS:
                        self._brightness -= 1
                        self._ra_fram.write_brightness(self._brightness)
                        self._display.set_brightness(self._brightness)

            return True

        if command == _CMD_BUTTON_4:
            # "UP" button

            if self._state not in [StateMachine.S_IDLE]:
                if self._programming:
                    self._program_up()
                else:
                    # Increase display brightness,
                    # and reset the timer.
                    self._to_idle_countdown = StateMachine.HOLD_TICKS
                    if self._brightness < _MAX_BRIGHTNESS:
                        self._brightness += 1
                        self._ra_fram.write_brightness(self._brightness)
                        self._display.set_brightness(self._brightness)

            return True

        # Something odd if we get here
        print(f'Command {command} not handled. Returning False.')
        return False

    def reset(self) -> None:
        """Called from the main loop to reset (stop) the machine.
        """
        if self._timer:
            self._timer.deinit()
            self._timer = None

    def _to_idle(self) -> bool:
        """Actions on entry to the IDLE state.
        """

        # Always clear any programming
        self._clear_program_mode()

        # Always set the new state
        self._state = StateMachine.S_IDLE
        # Initialise state variables
        self._display.clear()
        self._stop_timer()

        return True

    def _to_display_ra(self) -> bool:
        """Actions on entry to the DISPLAY_RA state.
        """

        # Always clear any programming
        self._clear_program_mode()

        # Always set the new state
        self._state = StateMachine.S_DISPLAY_RA
        # Initialise state variables
        self._start_timer()
        self._display.show_ra(self._ra_target, self._calibration_date)

        return True

    def _to_display_ra_target(self) -> bool:
        """Actions on entry to the DISPLAY_RA_TARGET state.
        """

        # Always clear any programming
        self._clear_program_mode()

        # Always set the new state
        self._state = StateMachine.S_DISPLAY_RA_TARGET
        # Initialise state variables
        self._start_timer()
        self._display.show_ra_target(self._ra_target)

        return True

    def _to_display_clock(self) -> bool:
        """Actions on entry to the DISPLAY_CLOCK state.
        """

        # Always clear any programming
        self._clear_program_mode()

        # Always set the new state
        self._state = StateMachine.S_DISPLAY_CLOCK
        # Initialise state variables
        self._start_timer()
        self._display.show_time()

        return True

    def _to_display_calibration_date(self) -> bool:
        """Actions on entry to the DISPLAY_TIME state.
        """

        # Always clear any programming
        self._clear_program_mode()

        # Always set the new state
        self._state = StateMachine.S_DISPLAY_C_DATE
        # Initialise state variables
        self._start_timer()
        self._display.show_calibration_date(self._calibration_date)

        return True

    def _to_program_ra_target_h(self) -> bool:

        # Always set the new state
        self._state = StateMachine.S_PROGRAM_RA_TARGET_H

        # Clear any countdown timer
        # While programming there is no idle countdown.
        self._to_idle_countdown = 0
        # Set programming mode
        self._programming = True
        self._programming_left = True
        self._programming_right = False
        self._programming_left_on = True
        self._programming_right_on = True
        self._programming_state = StateMachine.S_DISPLAY_RA_TARGET

        # Start the timer
        # (used to flash the appropriate part of the display)
        self._start_timer(to_idle=False)

        if not self._programming_value:
            # What is the value we're programming?
            ra_target: RA = self._ra_fram.read_ra_target()
            self._programming_value = f'{ra_target.h:02d}{ra_target.m:02d}'
            self._display.show(self._programming_value)

        return True

    def _to_program_ra_target_m(self) -> bool:

        # Always set the new state
        self._state = StateMachine.S_PROGRAM_RA_TARGET_M

        # Set programming mode
        self._programming_left = False
        self._programming_right = True

        return True

    def _to_program_clock(self) -> bool:

        # Always set the new state
        self._state = StateMachine.S_PROGRAM_CLOCK

        # Clear any countdown timer
        # While programming there is no idle countdown.
        self._to_idle_countdown = 0
        # Set programming mode
        self._programming = True
        self._programming_left = True
        self._programming_right = True
        self._programming_left_on = True
        self._programming_right_on = True
        self._programming_state = StateMachine.S_DISPLAY_CLOCK

        # Start the timer
        # (used to flash the appropriate part of the display)
        self._start_timer(to_idle=False)

        # What is the value we're programming?
        rtc_time: Tuple = self._rtc.datetime()
        # We just need HHMM...
        self._programming_value = f'{rtc_time[4]:02d}{rtc_time[5]:02d}'
        self._display.show(self._programming_value)

        return True

    def _to_program_calibration_day(self) -> bool:

        # Always set the new state
        self._state = StateMachine.S_PROGRAM_C_DAY

        # Clear any countdown timer
        # While programming there is no idle countdown.
        self._to_idle_countdown = 0
        # Set programming mode
        self._programming = True
        self._programming_left = True
        self._programming_right = False
        self._programming_left_on = True
        self._programming_right_on = True
        self._programming_state = StateMachine.S_DISPLAY_C_DATE

        # Start the timer
        # (used to flash the appropriate part of the display)
        self._start_timer(to_idle=False)

        if not self._programming_value:
            # What is the value we're programming?
            c_date: CalibrationDate = self._ra_fram.read_calibration_date()
            self._programming_value = f'{c_date.d:2d}{_MONTH_NAME[c_date.m]}'
            self._display.show(self._programming_value)

        return True

    def _to_program_calibration_month(self) -> bool:

        # Always set the new state
        self._state = StateMachine.S_PROGRAM_C_MONTH

        # Set programming mode
        self._programming_left = False
        self._programming_right = True

        return True


# Global Objects

# Our RTC object (RV3028 wrapper).
_RA_RTC: RaRTC = RaRTC()

# The LED display
# (using our real-time clock class)
_RA_DISPLAY: RaDisplay =\
    RaDisplay(_I2C, _RA_RTC, _DISPLAY_L_ADDRESS, _DISPLAY_R_ADDRESS)

# Create the FRAM and RaFRAM instance
_BASE_FRAM: BaseFRAM = BaseFRAM(_I2C, _FRAM_ADDRESS)
_RA_FRAM: RaFRAM = RaFRAM(_BASE_FRAM)

# Create the StateMachine instance
_STATE_MACHINE: StateMachine = StateMachine(_RA_DISPLAY, _RA_FRAM, _RA_RTC)
# Command 'queue'
_COMMAND_QUEUE: CommandQueue = CommandQueue()


def main() -> NoReturn:
    """The main application entrypoint.
    Called when _RUN is True and not expected to return.

    Here we spin the display (just to show the user we've started)
    and then sit in a loop waiting for a command (button-press or timer)
    passing each to the state machine that controls the display.

    The user can exit the main loop with a 'long' press of button 4 (UP).
    """

    _BUTTON_1.irq(trigger=Pin.IRQ_RISING, handler=btn_1)
    _BUTTON_2.irq(trigger=Pin.IRQ_RISING, handler=btn_2)
    _BUTTON_3.irq(trigger=Pin.IRQ_RISING, handler=btn_3)
    _BUTTON_4.irq(trigger=Pin.IRQ_RISING, handler=btn_4)

    _RA_DISPLAY.show('o   ')
    time.sleep_ms(250)
    _RA_DISPLAY.show(' o  ')
    time.sleep_ms(250)
    _RA_DISPLAY.show('  o ')
    time.sleep_ms(250)
    _RA_DISPLAY.show('   o')
    time.sleep_ms(250)
    _RA_DISPLAY.show('    ')
    time.sleep_ms(1_000)

    # Starting,
    # force initial display if compensated RA value...
    _COMMAND_QUEUE.put(_CMD_BUTTON_1)

    # Main loop
    while True:

        # Wait for a command
        cmd: Optional[int] = _COMMAND_QUEUE.get()
        while cmd is None:
            time.sleep_ms(250)  # type: ignore
            cmd = _COMMAND_QUEUE.get()

        assert cmd
        result: bool = False
        try:
            result = _STATE_MACHINE.process_command(cmd)
        except Exception as ex:
            print(f'StateMachine Exception "{ex}"')
        if not result:
            print('Got failure from StateMachine. Leaving.')
            break

    _RA_DISPLAY.show('Exit')

    # Reset the state machine...
    _STATE_MACHINE.reset()

    # Detach button callbacks
    _BUTTON_1.irq()
    _BUTTON_2.irq()
    _BUTTON_3.irq()
    _BUTTON_4.irq()

    _RA_DISPLAY.show('Done')


# Main ------------------------------------------------------------------------

if __name__ == '__main__':

    if _RUN:
        main()
