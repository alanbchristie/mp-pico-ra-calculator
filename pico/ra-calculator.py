"""The rea-time RA compensation calculator.

To clear any stored values in the FRAM run _RA_FRAM.clear().
"""
import time
try:
    from typing import Dict, List, Optional, Union
except ImportError:
    pass

# pylint: disable=import-error
import micropython  # type: ignore
from machine import I2C, Pin, Timer  # type: ignore
from ucollections import namedtuple # type: ignore

from pimoroni_i2c import PimoroniI2C  # type: ignore
from breakout_rtc import BreakoutRTC  # type: ignore

# Uncomment when debugging callback problems
micropython.alloc_emergency_exception_buf(100)

# The Pico on-board LED
ONBOARD_LED: Pin = Pin(25, Pin.OUT)

# An RA value: hours and minutes.
RA: namedtuple = namedtuple('RA', ('h', 'm'))
# A Calibration Date: day, month, year
CalibrationDate: namedtuple = namedtuple('CalibrationDate', ('d', 'm', 'y'))

# The target RA (Capell, the brightest star in the constellation of Auriga).
# This is the Right Ascension of the target object.
# If the scope is aligned south at midnight on the date of calibration,
# this will be the value required on its RA axis.
# We add the current time (if it's not midnight) to this value and
# 4 minutes for each day since the calibration.
DEFAULT_RA_TARGET: RA = RA(5, 16)

# The date the telescope's RA axis was calibrated.
# A tuple of day, month, year. We don't need the
# calibrated RA axis value, just the date it was calibrated.
# For each day beyond this (looping every 365 days) we add 4 minutes
# to the calibrated value.
DEFAULT_CALIBRATION_DATE: CalibrationDate = CalibrationDate(3, 1, 2022)

# Minutes in one day
_DAY_MINUTES: int = 1440

# Configured I2C Pins
_SCL: int = 17
_SDA: int = 16

# A Pimoroni i2c object (for supported devices)
_PINS: Dict[str, int] = {'scl': _SCL, 'sda': _SDA}
_PIMORONI_I2C: PimoroniI2C = PimoroniI2C(**_PINS)
# A MicroPython i2c object (for special/unsupported devices)
_I2C: I2C = I2C(id=0, scl=Pin(_SCL), sda=Pin(_SDA))

# Find the LED displays (LTP305 devcies) on 0x61, 0x62 or 0x63.
# We must have two and the first becomes the left-hand pair of digits
# (the hour) for the RA/Clock display.
_RA_DISPLAY_H_ADDRESS: Optional[int] = None
_RA_DISPLAY_M_ADDRESS: Optional[int] = None
_DEVICE_ADDRESSES: List[int] = _I2C.scan()
for device_address in _DEVICE_ADDRESSES:
    if device_address in [0x61, 0x62, 0x63]:
        if not _RA_DISPLAY_H_ADDRESS:
            # First goes to 'H'
            _RA_DISPLAY_H_ADDRESS = device_address
        elif not _RA_DISPLAY_M_ADDRESS:
            # Second goes to 'M'
            _RA_DISPLAY_M_ADDRESS = device_address
    if _RA_DISPLAY_M_ADDRESS:
        # We've set the 2nd device,
        # we can stop assinging
        break
assert _RA_DISPLAY_H_ADDRESS
assert _RA_DISPLAY_M_ADDRESS
print(f'RA.h device={hex(_RA_DISPLAY_H_ADDRESS)}')
print(f'RA.m device={hex(_RA_DISPLAY_M_ADDRESS)}')

# Did we have a Real-Time Clock (at 0x62)?
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

# Control buttion pin designation
_BUTTON_1: Pin = Pin(11, Pin.IN)
_BUTTON_2: Pin = Pin(12, Pin.IN)
_BUTTON_3: Pin = Pin(13, Pin.IN)
_BUTTON_4: Pin = Pin(14, Pin.IN)

# The period of time to sit in the
# button callback checking the button state.
# A simple form of debounce.
_BUTTON_DEBOUNCE_MS: int = 50


def is_leap_year(year) -> bool:
    """Returns True of the given year is a leap year.
    """
    if year % 4 == 0:
        if year % 100 == 0:
            if year % 400 == 0:
                return True
        else:
            return True

    return False


def days_between_dates(year1, month1, day1, year2, month2, day2) -> int:
    """Returns the number of days between the given dates where,
    in our usage, the earlier data (the calibration date) is passed in
    using the "1" values and the current date usign the "2" values.
    """
    # Cumulative Days by month
    cmtive_days = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    # Cumulative Days by month for leap year
    leap_cmtive_days = [0, 31, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335]
    tot_days = 0
    if year1 == year2:
        if is_leap_year(year1):
            return (leap_cmtive_days[month2 - 1] + day2) - \
                   (leap_cmtive_days[month1 - 1] + day1)
        return (cmtive_days[month2 - 1] + day2) - \
               (cmtive_days[month1 - 1] + day1)

    if is_leap_year(year1):
        tot_days = tot_days + 366 - (leap_cmtive_days[month1 - 1] + day1)
    else:
        tot_days = tot_days + 365 - (cmtive_days[month1 - 1] + day1)

    year = year1 + 1
    while year < year2:
        if is_leap_year(year):
            tot_days = tot_days + 366
        else:
            tot_days = tot_days + 365
        year = year + 1

    if is_leap_year(year2):
        tot_days = tot_days + (leap_cmtive_days[month2 - 1] + day2)
    else:
        tot_days = tot_days + (cmtive_days[month2 - 1] + day2)

    return tot_days


class FRAM:
    """Driver for the FRAM breakout.
    """

    def __init__(self, i2c, address: int = 0x50):
        """Create an instance for a devices at a given address.
        We can have eight devices, from 0x50 - 0x57.
        """
        assert i2c
        assert address
        assert address >= 0x50
        assert address <= 0x57

        self._i2c = i2c
        self._address = address

        print(f' FRAM initialised({hex(self._address)})')

    def write_byte(self, offset: int, byte_value: int) -> bool:
        """Writes a single value (expected to be a byte).
        For now it's assumed to be a +ve value (including zero), i.e. 0-127
        """
        # Max offset is 32K
        assert offset >= 0
        assert offset < 32_768
        assert byte_value >= 0
        assert byte_value < 128

        print(f' FRAM write_byte({offset}, {byte_value}) [{hex(self._address)}]')

        num_acks = self._i2c.writeto(self._address,
                                     bytes([offset >> 8,
                                            offset & 0xff,
                                            byte_value]))
        if num_acks != 3:
            print(f'Failed to write to FRAM at {self._address}.' +
                  f' Got {num_acks} acks, expected 3')

        return num_acks != 3

    def read_byte(self, offset) -> int:
        """Reads a single byte, assumed to be in the range 0-127,
        returning it as an int.
        """
        # Max offset is 32K
        assert offset >= 0
        assert offset < 32_768

        print(f' FRAM read_byte({offset}) [{hex(self._address)}]')

        num_acks = self._i2c.writeto(self._address,
                                     bytes([offset >> 8, offset & 0xff]))
        assert num_acks == 2
        got = self._i2c.readfrom(self._address, 1)
        assert got

        int_got: int = int.from_bytes(got, 'big')
        print(f' FRAM read_byte {int_got}')
        return int_got


class LTP305:
    """A simple class to control a LTP305 in MicroPython on a Pico. Based on
    Pimoroni's Raspberry Pi code at https://github.com/pimoroni/ltp305-python.
    Instead of using the Pi i2c library (which we can't use on the Pico)
    we use the MicroPython i2c library.

    The displays can use i2c address 0x61-0x63.
    """

    # LTP305 bitmaps for the basic 'ASCII' character set.
    font = {
        32: [0x00, 0x00, 0x00, 0x00, 0x00],  # (space)
        33: [0x00, 0x00, 0x5f, 0x00, 0x00],  # !
        34: [0x00, 0x07, 0x00, 0x07, 0x00],  # "
        35: [0x14, 0x7f, 0x14, 0x7f, 0x14],  # #
        36: [0x24, 0x2a, 0x7f, 0x2a, 0x12],  # $
        37: [0x23, 0x13, 0x08, 0x64, 0x62],  # %
        38: [0x36, 0x49, 0x55, 0x22, 0x50],  # &
        39: [0x00, 0x05, 0x03, 0x00, 0x00],  # '
        40: [0x00, 0x1c, 0x22, 0x41, 0x00],  # (
        41: [0x00, 0x41, 0x22, 0x1c, 0x00],  # )
        42: [0x08, 0x2a, 0x1c, 0x2a, 0x08],  # *
        43: [0x08, 0x08, 0x3e, 0x08, 0x08],  # +
        44: [0x00, 0x50, 0x30, 0x00, 0x00],  # ,
        45: [0x08, 0x08, 0x08, 0x08, 0x08],  # -
        46: [0x00, 0x60, 0x60, 0x00, 0x00],  # .
        47: [0x20, 0x10, 0x08, 0x04, 0x02],  # /
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
        58: [0x00, 0x36, 0x36, 0x00, 0x00],  # :
        59: [0x00, 0x56, 0x36, 0x00, 0x00],  # ;
        60: [0x00, 0x08, 0x14, 0x22, 0x41],  # <
        61: [0x14, 0x14, 0x14, 0x14, 0x14],  # =
        62: [0x41, 0x22, 0x14, 0x08, 0x00],  # >
        63: [0x02, 0x01, 0x51, 0x09, 0x06],  # ?
        64: [0x32, 0x49, 0x79, 0x41, 0x3e],  # @
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
        91: [0x00, 0x00, 0x7f, 0x41, 0x41],  # [
        92: [0x02, 0x04, 0x08, 0x10, 0x20],  # \
        93: [0x41, 0x41, 0x7f, 0x00, 0x00],  # ]
        94: [0x04, 0x02, 0x01, 0x02, 0x04],  # ^
        95: [0x40, 0x40, 0x40, 0x40, 0x40],  # _
        96: [0x00, 0x01, 0x02, 0x04, 0x00],  # `
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
        123: [0x00, 0x08, 0x36, 0x41, 0x00],  # {
        124: [0x00, 0x00, 0x7f, 0x00, 0x00],  # |
        125: [0x00, 0x41, 0x36, 0x08, 0x00],  # }
        126: [0x08, 0x08, 0x2a, 0x1c, 0x08],  # ~
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
        """Set brightness of both LED matrices.

        :param brightness: LED brightness from 0.0 to 1.0
        :param update: Push change to display immediately (otherwise you must call .show())

        """
        assert brightness >= 0.0
        assert brightness <= 1.0
        
        _brightness = int(brightness * 127.0)
        self._brightness = min(127, max(0, _brightness))
        if update:
            self._bus.writeto_mem(self._address,
                                  LTP305.CMD_BRIGHTNESS,
                                  self._brightness.to_bytes(1, 'big'))

    def set_decimal(self,
                    left: Optional[bool] = None,
                    right: Optional[bool] = None) -> None:
        """Set decimal of left and/or right matrix.

        :param left: State of left decimal dot
        :param right: State of right decimal dot

        """
        if left is not None:
            if left:
                self._buf_matrix_left[7] |= 0b01000000
            else:
                self._buf_matrix_left[7] &= 0b10111111
        if right is not None:
            if right:
                self._buf_matrix_right[6] |= 0b10000000
            else:
                self._buf_matrix_right[6] &= 0b01111111

    def set_pixel(self, px: int, py: int, val: int) -> None:
        """Set a single pixel on the matrix.
        """
        if px < 5:  # Left Matrix
            if val:
                self._buf_matrix_left[px] |= (0b1 << py)
            else:
                self._buf_matrix_left[px] &= ~(0b1 << py)
        else:      # Right Matrix
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

        :param x: x position, 0 for left, 5 for right, or in between if you fancy
        :param char: string character or char ordinal

        """
        if not isinstance(char, int):
            assert isinstance(char, str)
            char = ord(char)
        pixel_data: List[int] = LTP305.font[char]
        for px in range(5):
            for py in range(8):
                c = pixel_data[px] & (0b1 << py)
                self.set_pixel(x + px, py, c)

    def show(self) -> None:
        """Update the LED matrix from the buffer.
        """
        self._bus.writeto_mem(self._address,
                              LTP305.CMD_MATRIX_L,
                              bytearray(self._buf_matrix_left))
        self._bus.writeto_mem(self._address,
                              LTP305.CMD_MATRIX_R,
                              bytearray(self._buf_matrix_right))
        self._bus.writeto_mem(self._address,
                              LTP305.CMD_MODE,
                              LTP305.MODE.to_bytes(1, 'big'))
        self._bus.writeto_mem(self._address,
                              LTP305.CMD_OPTIONS,
                              LTP305.OPTS.to_bytes(1, 'big'))
        self._bus.writeto_mem(self._address,
                              LTP305.CMD_BRIGHTNESS,
                              self._brightness.to_bytes(1, 'big'))
        self._bus.writeto_mem(self._address,
                              LTP305.CMD_UPDATE,
                              LTP305.UPDATE.to_bytes(1, 'big'))
    

class LTP305_Pair:
    """A wrapper around two LTP305 objects to form a Right-Ascension display.
    Basically a clock, but given "[HH][mm]". The dsiaply can also be used
    to display the target RA, the current time, the calibration date, and
    year.
    """
    
    def __init__(self,
                 i2c,
                 rtc,
                 address_h: int,
                 address_m: int,
                 brightness: int = _MIN_BRIGHTNESS):
        """Initialises the RA object, given a i2c instance and optional
        display addresses and brightness.
        """
        assert i2c
        assert rtc
        assert brightness >= _MIN_BRIGHTNESS
        assert brightness <= _MAX_BRIGHTNESS
        
        self._rtc = rtc
        self._brightness: float = brightness / _MAX_BRIGHTNESS
        
        self.h_matrix = LTP305(i2c, address=address_h, brightness=self._brightness)
        self.m_matrix = LTP305(i2c, address=address_m, brightness=self._brightness)
        
    def set_brightness(self, brightness: int) -> None:
        assert brightness >= _MIN_BRIGHTNESS
        assert brightness <= _MAX_BRIGHTNESS

        # Remember this setting
        self._brightness = brightness / _MAX_BRIGHTNESS

        self.h_matrix.set_brightness(self._brightness, True)
        self.m_matrix.set_brightness(self._brightness, True)

    def clear(self, left: bool = True, right: bool = True) -> None:
        if left:
            self.h_matrix.clear()
            self.h_matrix.show()

        if right:
            self.m_matrix.clear()    
            self.m_matrix.show()

    def show_ra(self, ra_target: RA, calibration_date: CalibrationDate)\
               -> None:
        """Uses the RTC to calculate the corrected RA value for the
        given RA target value and its calibration date (with defaults).
        """
        assert ra_target
        assert calibration_date

        # Read the RTC until we get something
        new_time: str = ''
        new_date: str = ''
        while not new_time and not new_date:
            if self._rtc.update_time():
                new_time = self._rtc.string_time()
                new_date = self._rtc.string_date()
            else:
                # Sleep (less than a second)
                time.sleep(0.5)
        
        # The time is given to us as 'HH:MM:SS',
        # we just need HH:MM, which we'll call 'clock'.
        clock: str = new_time[:5]

        # Calculate the corrected RA.
        #
        # First, We add the current time to the target RA.
        target_ra_minutes: int = ra_target.h * 60 + ra_target.m
        clock_hours: int = int(clock[:2])
        clock_minutes: int = clock_hours * 60 + int(clock[3:])
        scope_ra_minutes: int = target_ra_minutes + clock_minutes
        # Then, we add 1 minute for every 6 hours on the clock.
        # i.e. after every 6 hours the celestial bodies will move by 1 minute.
        # This accomodates the sky's progression for the current day.
        sub_day_offset: int = clock_hours // 6
        scope_ra_minutes += sub_day_offset
        # Then, add 4 minutes for each whole day since calibration.
        # The RTC date format is 'dd/mm/yyyy'.
        # The celestial bodies drift by 4 minutes per day
        # (essentially that's what the extra day in the leap-year is all about).
        # The maximum correction is 364 days. After each year we're back to
        # a daily offset of '0'.
        date_day: int = int(new_date[:2])
        date_month: int = int(new_date[3:5])
        date_year: int = int(new_date[6:])
        elapsed_days: int = days_between_dates(calibration_date.y,
                                               calibration_date.m,
                                               calibration_date.d,
                                               date_year,
                                               date_month,
                                               date_day)
        assert elapsed_days >= 0
        # If caliration was on the 4th and today is the 5th the days between the
        # dates is '1' but, the first 24 hours is handled by the
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

        clock: str = ''

        if not hour or not minute:
            # Read the RTC until we get something
            new_time: str = ''
            while not new_time:
                if self._rtc.update_time():
                    new_time = self._rtc.string_time()
                else:
                    # Sleep (less than a second)
                    time.sleep(0.5)
            # The time is given to us as 'HH:MM:SS',
            # we just need HH:MM, which we'll call 'clock'.
            clock: str = new_time[:2] + new_time[3:5]
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
        """
        clock: str = f'{calibration_date.d}'
        if calibration_date.d < 10:
            # Pad with space
            clock = ' ' + clock
        if calibration_date.m < 10:
            clock += f' {calibration_date.m}'
        else:
            clock += f'{calibration_date.m}'

        # Display
        self.show(clock)

    def show_calibration_year(self, calibration_date) -> None:
        """Displays the current calibration_year.
        """
        # The time is given to us as 'HH:MM:SS',
        # we just need HH:MM, which we'll call 'clock'.
        clock: str = f'{calibration_date.y:04d}'
        # Display
        self.show(clock)

    def show(self, value: str) -> None:
        """Set the display, given a 4-digit string '[HH][MM]'.
        """
        assert isinstance(value, str)
        assert len(value) == 4

        # Hour [HH] (leading zero replaced by ' ')
        if value[0] == '0':
            self.h_matrix.set_pair(' ' + value[1:2])
        else:
            self.h_matrix.set_pair(value[:2])
        # Minute [MM]
        self.m_matrix.set_pair(value[2:])

        self.h_matrix.show()
        self.m_matrix.show()


class RA_FRAM:
    """A RA wrapper around a FRAM class. This class provides
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

    # Markers
    # Values that prefix every stored value. These are used to indicate
    # whether the value that follows is valid or invalid.
    # Markers (and data values) must be +ve byte values (0-127)
    _INVALID: int = 0
    _VALID: int = 33

    # Memory Map
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
    # |     6  | Calibration Date (Day) [0..31]
    # |     7  | Calibration Date (Month) [1..12]
    # |     8  | Calibration Date (Century) [20..]
    # |     9  | Calibration Date (Year) [0..99]
    # +--------+----------------------------------
    _OFFSET_BRIGHTNESS: int = 0       # 1 value 
    _OFFSET_RA_TARGET: int = 2        # 2 values
    _OFFSET_CALIBRATION_DATE: int = 5 # 4 values

    def __init__(self, fram):
        # Save the FRAM reference
        self._fram = fram
        
        # Cached values of data.
        # Set when reading or writing the corresponding values.
        self._ra_target: Optional[RA] = None
        self._brightness: Optional[int] = None
        self._calibration_date: Optional[CalibrationDate] = None
    
    def _write_value(self, offset: int, value: Union[int, List[int]]) -> None:
        assert offset >= 0
        
        # Set marker to invalid
        # Write value (or values)
        # Set marker to valid
        print(f'RA_FRAM Write {value} @{offset}')
        self._fram.write_byte(offset, RA_FRAM._INVALID)
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
        self._fram.write_byte(offset, RA_FRAM._VALID)
        
    def _read_value(self, offset: int) -> int:
        """Reads the value form the offset.
        The offset provided is the marker, we read the value
        after the marker.
        """
        assert offset >= 0
        assert offset + 1 < 32_768
        
        value: int = self._fram.read_byte(offset + 1)
        print(f'RA_FRAM Read {value} @ {hex(offset)}')
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
        print(f'RA_FRAM Read {value} @ {hex(offset)}')
        assert len(value) == length

        return value
        
    def _is_value_valid(self, offset: int) -> bool:
        assert offset >= 0
        
        byte_value: int = self._fram.read_byte(offset)
        value: bool = byte_value == RA_FRAM._VALID
        print(f'RA_FRAM IsValid @{hex(offset)} [{byte_value}] {value}')

        return value
        
    def read_brightness(self) -> int:

        print('RA_FRAM read_brightness()...')

        # Return the cached (last written) value if we have it
        if self._brightness:
            return self._brightness
        # Is there a value in the FRAM?
        # If so, read it, put it in the cache and return it.
        if self._is_value_valid(RA_FRAM._OFFSET_BRIGHTNESS):
            self._brightness = self._read_value(RA_FRAM._OFFSET_BRIGHTNESS)
            return self._brightness
        # No cached value, no stored value,
        # so write and then return the default
        self.write_brightness(RA_FRAM.DEFAULT_BRIGHTNESS)
        assert self._brightness
        return self._brightness
    
    def write_brightness(self, brightness: int) -> None:
        assert brightness >= _MIN_BRIGHTNESS
        assert brightness <= _MAX_BRIGHTNESS
        
        print(f'RA_FRAM write_brightness({brightness})...')

        # Write to FRAM
        self._write_value(RA_FRAM._OFFSET_BRIGHTNESS, brightness)

        # Finally, save the value to the cached value
        self._brightness = brightness
    
    def read_ra_target(self) -> RA:

        print('RA_FRAM read_ra_target()...')
        
        # Return the cached (last written) value if we have it
        if self._ra_target:
            return self._ra_target
        # Is there a value in the FRAM?
        # If so, read it, put it in the cache and return it.
        if self._is_value_valid(RA_FRAM._OFFSET_RA_TARGET):
            value: List[int] = self._read_values(RA_FRAM._OFFSET_RA_TARGET, 2)
            self._ra_target = RA(value[0], value[1])
            return self._ra_target
        # No cached value,
        # so write and then return the default
        self.write_ra_target(DEFAULT_RA_TARGET)
        return self._ra_target
    
    def write_ra_target(self, ra_target: RA) -> None:
        assert ra_target

        print(f'RA_FRAM write_ra_target({ra_target})...')

        # Write to FRAM
        values: List[int] = [ra_target.h, ra_target.m]
        self._write_value(RA_FRAM._OFFSET_RA_TARGET, values)

        # Finally, save the value to the cached value
        self._ra_target = ra_target
    
    def read_calibration_date(self) -> CalibrationDate:

        print('RA_FRAM read_calibration_date()...')

        # Return the cached (last written) value if we have it
        if self._calibration_date:
            return self._calibration_date
        # Is there a value in the FRAM?
        # If so, read it, put it in the cache and return it.
        if self._is_value_valid(RA_FRAM._OFFSET_CALIBRATION_DATE):
            value: List[int] = self._read_values(RA_FRAM._OFFSET_CALIBRATION_DATE, 4)
            year: int = value[2] * 100 + value[3]
            self._calibration_date = CalibrationDate(value[0], value[1], year)
            return self._calibration_date
        # No cached value,
        # so write and then return the default
        self.write_calibration_date(DEFAULT_CALIBRATION_DATE)
        return self._calibration_date
    
    def write_calibration_date(self, calibration_date: CalibrationDate) -> None:
        assert calibration_date

        print(f'RA_FRAM write_calibration_date({calibration_date})...')

        # Write to FRAN
        century: int = calibration_date.y // 100
        year: int = calibration_date.y % 100
        values: List[int] = [calibration_date.d, calibration_date.m, century, year]
        self._write_value(RA_FRAM._OFFSET_CALIBRATION_DATE, values)
        
        # Finally, save the value to the cached value
        self._calibration_date = calibration_date
    
    def clear(self):
        """Clears, invalidates, the RA FRAM values.
        """
        print('RA_FRAM clear()...')

        self._fram.write_byte(RA_FRAM._OFFSET_BRIGHTNESS, RA_FRAM._INVALID)
        self._fram.write_byte(RA_FRAM._OFFSET_RA_TARGET, RA_FRAM._INVALID)
        self._fram.write_byte(RA_FRAM._OFFSET_CALIBRATION_DATE, RA_FRAM._INVALID)


# The command queue - the object between the
# buttons, timers and the main-loop state machine.
class CommandQueue:

    def __init__(self):
        self._queue_size: int = 100
        self._queue = []
    
    def members(self) -> int:
        return len(self._queue)

    def put(self, command: int) -> None:
        if len(self._queue) < self._queue_size:
            self._queue.append(command)
    
    def get(self) -> Optional[int]:
        if self.members():
            return self._queue.pop(0)
        return None


# CommandQueue commands (just unique integers)
_CMD_TICK: int = 1      # The 1-second ticker has fired
_CMD_BUTTON_1: int = 2  # Button 1 has been pressed
_CMD_BUTTON_2: int = 3  # Button 2 has been pressed
_CMD_BUTTON_3: int = 4  # Button 3 has been pressed
_CMD_BUTTON_4: int = 5  # Button 4 has been pressed
    
# Create a real-time clock object
# (using the Pimoroni library)
_RTC: BreakoutRTC = BreakoutRTC(_PIMORONI_I2C)

# Create the RA display object
# (using the built-in MicroPython library)
_RA_DISPLAY: LTP305_Pair =\
    LTP305_Pair(_I2C, _RTC, _RA_DISPLAY_H_ADDRESS, _RA_DISPLAY_M_ADDRESS)


def btn_1(pin: Pin) -> None:
    """The '**DISPLAY** button. Pressing this when the display is off
    will disply the current (real-time) RA axis compensataion value.
    When the display is on it cycles between this and displaying the target RA,
    The current time, the calibration day and month and the calibration year.
    """

    # Crude debounce.
    # We disable theis pin's interrupt and
    # pause here for a debounce period. If the pin is still pressed
    # after we wake up then we can safely react to the button.
    pin.irq(handler=None)
    # pylint: disable=no-member
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    if pin.value():
        _COMMAND_QUEUE.put(_CMD_BUTTON_1)
    pin.irq(handler=btn_1)


def btn_2(pin: Pin) -> None:
    """The **PROGRAM** button. Pressing this when the display is on allows
    adjustments to the displayed value. The compensated RA value cannot
    be adjusted. The taregt RA is calculated automatically from the current time
    and calibration date. When pressed during the display of target RA, current
    time or clibration values the values flash and the UP/DOWN buttons
    can be used to alter the displayed value.

    Pressing the program button again saves the value. Presssing MODE cancels
    the change.
    """

    pin.irq(handler=None)
    # pylint: disable=no-member
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    if pin.value():
        _COMMAND_QUEUE.put(_CMD_BUTTON_2)
    pin.irq(handler=btn_2)


def btn_3(pin: Pin) -> None:
    """The **DOWN** button. Pressing this when the display is on decreases
    the display brightness.
    """

    pin.irq(handler=None)
    # pylint: disable=no-member
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    if pin.value():
        _COMMAND_QUEUE.put(_CMD_BUTTON_3)
    pin.irq(handler=btn_3)
        

def btn_4(pin: Pin) -> None:
    """The **UP** button. Pressing this when the display is on increases
    the display brightness.
    """

    pin.irq(handler=None)
    # pylint: disable=no-member
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    if pin.value():
        _COMMAND_QUEUE.put(_CMD_BUTTON_4)
    pin.irq(handler=btn_4)


def tick(timer):
    """A timer callback, called once per second.
    Simply inserts a tick into the command queue.
    """
    assert timer

    _COMMAND_QUEUE.put(_CMD_TICK)
    

class StateMachine:
    # The states
    S_IDLE: int = 0
    S_DISPLAY_RA: int = 1
    S_DISPLAY_RA_TARGET: int = 2
    S_DISPLAY_CLOCK: int = 3
    S_DISPLAY_C_DATE: int = 4
    S_DISPLAY_C_YEAR: int = 5
    S_PROGRAM_RA_TARGET_H: int = 6
    S_PROGRAM_RA_TARGET_M: int = 7
    S_PROGRAM_CLOCK: int = 8
    S_PROGRAM_C_DAY: int = 9
    S_PROGRAM_C_MONTH: int = 10
    S_PROGRAM_C_YEAR: int = 11
    
    TIMER_PERIOD_MS: int = 500
    # Number of timer ticks to hold the display before returning to idle
    # (8 is 4 seconds when the timer is 500mS)
    HOLD_TICKS: int = 8
    
    def __init__(self, display: LTP305_Pair, ra_fram: RA_FRAM):
        assert display
        assert ra_fram
        
        # The current state
        self._state: int = StateMachine.S_IDLE
        # A countdown timer.
        # The timer takes this down to zero.
        # When it reaches zero the display is cleared (mode returns to IDLE)
        self._to_idle_countdown: int = 0
        # The RA display and FRAM
        self._display: LTP305_Pair = display
        self._ra_fram: RA_FRAM = ra_fram
        # Brightness
        self._brightness: int = self._ra_fram.read_brightness()
        self._ra_target: RA = self._ra_fram.read_ra_target()
        self._calibration_date: CalibrationDate = self._ra_fram.read_calibration_date()
        self._display.set_brightness(self._brightness)
        
        # A Timer object.
        # Initialised when something is displayed.
        # De-initialised when the display is cleared.
        # The timer runs outside the context of this object,
        # we just enable and disable it.
        self._timer: Optional[Timer] = None
        
        # Program mode indication.
        # When in program mode the display flashes.
        # Whether the left, right or left and right displays flash
        # will depend on the state we're in.
        self._programming: bool = False
        self._programming_left: bool = False
        self._programming_right: bool = False
        # The state that's being programmed.
        # We use this to return to remember which state to return to
        # when programming is finished or cancelled.
        self._programming_state: int = StateMachine.S_IDLE
        # Current visibility of left and right digit-pair
        self._programming_left_on: bool = False
        self._programming_right_on: bool = False
        # The value to present to the display when programming.
        self._programming_value: str = ''

    def _clear_program_mode(self) -> None:
        self._programming = False

    def _start_timer(self, to_idle: bool = True) -> None:
        """Starts the timer.
        If to_idle is True the timer is a countdown to idle.
        where the mode will rever to idle when the countdown is complete.
        """
        if self._timer is None:
            self._timer = Timer(period=StateMachine.TIMER_PERIOD_MS, callback=tick)
        if to_idle:
            self._to_idle_countdown = StateMachine.HOLD_TICKS
            
    def _stop_timer(self) -> None:
        if self._timer:
            self._timer.deinit()
            self._timer = None
        self._to_idle_countdown = 0
            
    def process_command(self, command: int) -> bool:
        """Process a command, where the actions depend on the
        current 'state'.
        """

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

            # Otherwise nothing to do
            return True

        if command == _CMD_BUTTON_1:
            # "DISPLAY" button

            # If not in programming mode we switch to another item to display.
            # Here we cancel programming mode if it's set
            # and then enter the normal mode of the item beign displayed.

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
                return self._to_display_calibration_year()
            if self._state == StateMachine.S_DISPLAY_C_YEAR:
                return self._to_display_ra()

            # Programming states
            if self._programming:
                # Switch programming off.
                # Return to the state that was being programmed...
                self._programming = False
                if self._programming_state == StateMachine.S_DISPLAY_CLOCK:
                    return self._to_display_clock()
                if self._programming_state == StateMachine.S_DISPLAY_C_YEAR:
                    return self._to_display_calibration_year()

            # Otherwise nothing to do
            return True

        if command == _CMD_BUTTON_2:
            # "PROGRAM" button

            # Into programming mode (from valid non-programming states)
            if self._state == StateMachine.S_DISPLAY_RA_TARGET:
                print('PROGRAM (ra target) [TBD]')
            if self._state == StateMachine.S_DISPLAY_CLOCK:
                return self._to_program_clock()             
            if self._state == StateMachine.S_DISPLAY_C_DATE:
                print('PROGRAM (calibration month) [TBD]')
            if self._state == StateMachine.S_DISPLAY_C_YEAR:
                return self._to_program_calibration_year()             

            # Out of programming mode (from programming states)
            # Here we commit the change and return to normal mode.
            # TODO
                
            # Otherwise nothing to do
            return True

        if command == _CMD_BUTTON_3:
            # "DOWN" button

            if not self._state in [StateMachine.S_IDLE]:
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

            if not self._state in [StateMachine.S_IDLE]:
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
        """Called fromt he main loop to reset (stop) the machine.
        """
        if self._timer:
            self._timer.deinit()
            self._timer = None
            
    def _to_idle(self) -> bool:
        """Actions on entry to the IDLE state.
        """
        print('_to_idle()')
        
        # ALways clear any programming
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
        print('_to_display_ra()')
        
        # ALways clear any programming
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
        print('_to_display_ra_target()')
        
        # ALways clear any programming
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
        print('_to_display_clock()')
        
        # ALways clear any programming
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
        print('_to_display_calibration_date()')
        
        # ALways clear any programming
        self._clear_program_mode()
        
        # Always set the new state
        self._state = StateMachine.S_DISPLAY_C_DATE
        # Initialise state variables
        self._start_timer()
        self._display.show_calibration_date(self._calibration_date)

        return True

    def _to_display_calibration_year(self) -> bool:
        """Actions on entry to the DISPLAY_TIME state.
        """
        print('_to_display_calibration_year()')
        
        # ALways clear any programming
        self._clear_program_mode()
        
        # Always set the new state
        self._state = StateMachine.S_DISPLAY_C_YEAR
        # Initialise state variables
        self._start_timer()
        self._display.show_calibration_year(self._calibration_date)

        return True

    def _to_program_clock(self) -> bool:
        
        print('_to_program_clock()')

        # Always set the new state
        self._state = StateMachine.S_PROGRAM_CLOCK
        
        # Clear any countdown timer
        # While programming there is no idle countdown.
        self._to_idle_countdown = 0
        # Set prigramming mode
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
        self._programming_value = '2222'
        self._display.show(self._programming_value)
        
        return True

    def _to_program_calibration_year(self) -> bool:
        
        print('_to_program_calibration_year()')

        # Always set the new state
        self._state = StateMachine.S_PROGRAM_C_YEAR
        
        # Clear any countdown timer
        # While programming there is no idle countdown.
        self._to_idle_countdown = 0
        # Set prigramming mode
        self._programming = True
        self._programming_left = True
        self._programming_right = True
        self._programming_left_on = True
        self._programming_right_on = True
        self._programming_state = StateMachine.S_DISPLAY_C_YEAR

        # Start the timer
        # (used to flash the appropriate part of the display)
        self._start_timer(to_idle=False)

        # What is the value we're programming?
        self._programming_value = '3333'
        self._display.show(self._programming_value)

        return True

# Main ------------------------------------------------------------------------

if __name__ == '__main__':

    # Switch on the on-board LED
    ONBOARD_LED.value(1)
    
    # Create the FRAM instance
    _FRAM: FRAM = FRAM(_I2C, _FRAM_ADDRESS)
    _RA_FRAM: RA_FRAM = RA_FRAM(_FRAM)
    # Create the StateMachine instance
    _STATE_MACHINE: StateMachine = StateMachine(_RA_DISPLAY, _RA_FRAM)
    # Command 'queue'
    _COMMAND_QUEUE: CommandQueue = CommandQueue()
    # Inject an automatic 'button-1' command into the command-queue
    # (puts the display on) and then wait for others,
    # leaving if the state machine fails
    _COMMAND_QUEUE.put(_CMD_BUTTON_1)

    # Attach button clicks to callbacks
    _BUTTON_1.irq(trigger=Pin.IRQ_FALLING, handler=btn_1)
    _BUTTON_2.irq(trigger=Pin.IRQ_FALLING, handler=btn_2)
    _BUTTON_3.irq(trigger=Pin.IRQ_FALLING, handler=btn_3)
    _BUTTON_4.irq(trigger=Pin.IRQ_FALLING, handler=btn_4)

    # Main loop
    while True:

        if _COMMAND_QUEUE.members():
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

    # Reset the state machine...
    _STATE_MACHINE.reset()
