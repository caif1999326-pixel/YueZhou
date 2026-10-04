"""Requested speeds; source-specific caps and HTTP backoff still take priority."""

SPEEDS = {
    '加速': dict(workers=6, interval=200),
    '标准': dict(workers=3, interval=400),
}
DEFAULT_SPEED = '加速'


def speed_options(name):
    return dict(SPEEDS.get(name, SPEEDS[DEFAULT_SPEED]))
