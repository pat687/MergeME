"""Which state a ZIP code is in, from its first three digits (the USPS ZIP3 prefix).

Used so the launch gate can't be passed by typing an open state with a ZIP code from a
closed one. Prefixes for military mail and territories, and the few that aren't
allocated, return None, and the declared state is used on its own.
"""

from __future__ import annotations

# (first prefix, last prefix, state), inclusive.
_RANGES = [
    (5, 5, "NY"),
    (6, 7, "PR"),
    (9, 9, "PR"),
    (10, 27, "MA"),
    (28, 29, "RI"),
    (30, 38, "NH"),
    (39, 49, "ME"),
    (50, 54, "VT"),
    (55, 55, "MA"),
    (56, 59, "VT"),
    (60, 69, "CT"),
    (70, 89, "NJ"),
    (100, 149, "NY"),
    (150, 196, "PA"),
    (197, 199, "DE"),
    (200, 200, "DC"),
    (201, 201, "VA"),
    (202, 205, "DC"),
    (206, 219, "MD"),
    (220, 246, "VA"),
    (247, 268, "WV"),
    (270, 289, "NC"),
    (290, 299, "SC"),
    (300, 319, "GA"),
    (320, 339, "FL"),
    (341, 349, "FL"),
    (350, 369, "AL"),
    (370, 385, "TN"),
    (386, 397, "MS"),
    (398, 399, "GA"),
    (400, 427, "KY"),
    (430, 459, "OH"),
    (460, 479, "IN"),
    (480, 499, "MI"),
    (500, 528, "IA"),
    (530, 549, "WI"),
    (550, 567, "MN"),
    (569, 569, "DC"),
    (570, 577, "SD"),
    (580, 588, "ND"),
    (590, 599, "MT"),
    (600, 629, "IL"),
    (630, 658, "MO"),
    (660, 679, "KS"),
    (680, 693, "NE"),
    (700, 714, "LA"),
    (716, 729, "AR"),
    (730, 732, "OK"),
    (733, 733, "TX"),
    (734, 749, "OK"),
    (750, 799, "TX"),
    (800, 816, "CO"),
    (820, 831, "WY"),
    (832, 838, "ID"),
    (840, 847, "UT"),
    (850, 865, "AZ"),
    (870, 884, "NM"),
    (885, 885, "TX"),
    (889, 898, "NV"),
    (900, 961, "CA"),
    (967, 968, "HI"),
    (970, 979, "OR"),
    (980, 994, "WA"),
    (995, 999, "AK"),
]


def state_for_zip(zip_code: str) -> str | None:
    """'60614' -> 'IL'. None when the prefix isn't a state's (or the input isn't a ZIP)."""
    if len(zip_code) < 3 or not zip_code[:3].isdigit():
        return None
    prefix = int(zip_code[:3])
    for low, high, state in _RANGES:
        if low <= prefix <= high:
            return state
    return None
