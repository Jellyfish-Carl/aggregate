CONTRACT_PERIODS = 48
PERIODS = 96
QUARTER_HOURS = 0.25


def quarter_time(period: int) -> str:
    if not 1 <= period <= PERIODS:
        raise ValueError("现货时段必须位于 [1, 96]")
    minutes = (period - 1) * 15
    return "%02d:%02d" % (minutes // 60, minutes % 60)
