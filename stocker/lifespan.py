"""Display-only lifespan grouping. No changes to inventory or forecasting rules."""
import math


def lifespan_group(days):
    """Positive days: <=30 Short, <=180 Medium, otherwise Long; unknown stays Unknown."""
    if days is None or isinstance(days, bool):
        return "Unknown"
    try:
        value = float(days)
    except (TypeError, ValueError):
        return "Unknown"
    if not math.isfinite(value) or value <= 0:
        return "Unknown"
    return "Short" if value <= 30 else "Medium" if value <= 180 else "Long"
