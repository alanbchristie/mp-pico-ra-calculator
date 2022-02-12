import time
try:
    from typing import Dict, List, NoReturn, Optional, Tuple, Union
except ImportError:
    pass

import micropython  # type: ignore
from machine import I2C, Pin  # type: ignore

# Uncomment when debugging callback problems
micropython.alloc_emergency_exception_buf(100)

# The Pico on-board LED
_ONBOARD_LED: Pin = Pin(25, Pin.OUT)

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

# Control button pin designation
# We don't need a 'Pin.PULL_UP'
# because the buttons on the 'Pico Breadboard'
# are pulled down.
_BUTTON_1: Pin = Pin(11, Pin.IN)
_BUTTON_2: Pin = Pin(12, Pin.IN)
_BUTTON_3: Pin = Pin(13, Pin.IN)
_BUTTON_4: Pin = Pin(14, Pin.IN)

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
    # Less than 3 seconds we insert a _CMD_BUTTON_2 command,
    # for 3 seconds or more it's a _CMD_BUTTON_2_LONG command.
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
    """The **UP** button. Creates the _CMD_BUTTON_4 command.

    Pressing this when the display is on increases
    the display brightness. In programming mode it increases the value that
    is flashing.
    """

    pin.irq(handler=None)
    time.sleep_ms(_BUTTON_DEBOUNCE_MS)  # type: ignore
    if pin.value():
        _COMMAND_QUEUE.put(_CMD_BUTTON_4)
    pin.irq(trigger=Pin.IRQ_RISING, handler=btn_4)


# Command 'queue'
_COMMAND_QUEUE: CommandQueue = CommandQueue()


def main() -> NoReturn:
    """The main application entrypoint - main.
    Called when _RUN is True and not expected to return.
    """

    # Onboard LED off...
    _ONBOARD_LED.value(1)
    _BUTTON_1.irq(trigger=Pin.IRQ_RISING, handler=btn_1)

    print('Waiting for button...')

    # What for user to press the button before extinguishing the LED
    button_hit: bool = False
    while not button_hit:
        if _COMMAND_QUEUE.get() == _CMD_BUTTON_1:
            button_hit = True
        else:
            time.sleep(1)

    print('Pressed!')

    # Onboard LED off...
    _ONBOARD_LED.value(0)


# Main ------------------------------------------------------------------------

if __name__ == '__main__':

    main()
