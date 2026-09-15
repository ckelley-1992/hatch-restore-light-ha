"""Sound catalog for the Hatch Restore Gen 1 (``product=restore``).

Generated from Hatch's ``service/app/content/v1/fetchByProduct?product=restore&contentTypes=sound``
response (2026-09-15). The live catalog is fetched at startup and merged over this table, so this is
the fallback when that request is rate-limited or unavailable. Ids are the ``sound.id`` values the
device reports in its shadow; ``10040`` (Light Rain) is what the device reports while idle.
"""

from __future__ import annotations

# Sleep sounds the Hatch app shows for the Restore Gen 1, in the app's display order.
RESTORE_SLEEP_SOUNDS: dict[int, str] = {
    10081: "Night's Tranquility",
    10071: "Sea Wind",
    10027: "Chirping Birds",
    10064: "Mountain Alps",
    10070: "Arctic Wind",
    10079: "Forest Song",
    10085: "Evening Campfire",
    10089: "Snowstorm",
    10040: "Light Rain",
    10039: "Heavy Rain",
    10045: "Spring Rain",
    10051: "Rain on Leaves",
    10054: "Calm Ocean",
    10059: "River Creek",
    10062: "Small Waterfall",
    10066: "Mountain Waterfall",
    10068: "Mighty River",
    10022: "Sea Waves",
    10055: "Sunny Beach",
    10117: "Tranquil Beach",
    10036: "Pink Noise",
    10033: "White Noise",
    10034: "Soft White Noise",
    10037: "Soft Pink Noise",
    10019: "Fan",
    10086: "Fireplace",
    10113: "Washer",
    10115: "Dishwasher",
    10096: "Wind Chimes",
    10102: "Thin Singing Bowl",
    10106: "Tibetan Bells",
}

# Sounds the app offers only for the sunrise alarm.
RESTORE_ALARM_SOUNDS: dict[int, str] = {
    10028: "Morning Bird",
    10029: "Bells",
    10030: "Lovely Chimes",
    10031: "Relaxing Chimes",
    10104: "Chinese Bells",
    10116: "Meditative Flute",
    10024: "Beep Beep",
    10025: "Signal",
    10026: "Retro Alarm",
    10094: "Old Clock Variations",
}

# Catalog entries the app hides (older variants such as the original Pink Noise, id 10003). They may
# or may not still exist on the device; kept so a reported id can be named.
RESTORE_HIDDEN_SOUNDS: dict[int, str] = {
    10001: "Heartbeat",
    10002: "Water Stream",
    10003: "Pink Noise",
    10004: "Dryer",
    10005: "Ocean Waves",
    10006: "Wind",
    10007: "Rain",
    10009: "Birds' Chorus",
    10010: "Crickets",
    10011: "Brahm's Lullaby",
    10013: "Twinkle Twinkle Little Star",
    10014: "Rockabye",
    10018: "Eternal Space",
    10020: "Mom's Heartbeat",
    10021: "Owls",
    10023: "Wooden Wind Chimes",
    10032: "Healing Sounds",
    10035: "Brown Noise",
    10038: "Airplane Cabin",
    10041: "Urban Rain",
    10042: "Forest Rain",
    10043: "Tropical Rain",
    10044: "Summer Night",
    10046: "Frozen Rain",
    10047: "Rainstorm",
    10048: "Shower",
    10049: "Rain on Roof",
    10050: "Rain on Car",
    10052: "Rain on Lake",
    10053: "Rain on Canvas",
    10056: "Calm Sea",
    10057: "Raging Sea",
    10058: "Mountain Stream",
    10060: "Morning on the River",
    10061: "Wild River",
    10063: "Cascading Waterfall",
    10065: "Forest Waterfall",
    10067: "Wide RIver",
    10069: "Lazy River",
    10072: "Wind in City Park",
    10073: "Wind Outside the Tent",
    10074: "Sandstorm",
    10075: "Highway",
    10076: "Birds in City Park",
    10077: "Village Birds",
    10078: "Sparrows Tweeting",
    10080: "Calm Summer Night",
    10082: "Forest Lake",
    10083: "Woodland Birds",
    10084: "Swamp",
    10087: "Warm Winter Night",
    10088: "Blizzard",
    10090: "Vacuum Cleaner",
    10091: "Industrial Vacuum Cleaner",
    10092: "Hair Dryer",
    10093: "Sprinkler",
    10095: "Crowd",
    10097: "Large Bamboo Wind Chimes",
    10098: "Bamboo Wind Chimes",
    10099: "Quiet Singing Bowl",
    10100: "Medium Singing Bowl",
    10101: "Big Singing Bowl",
    10103: "Forged Singing Bowl",
    10105: "Tibetan Singing Bowl",
    10107: "Low Singing Bowl",
    10108: "Heartbeat 75 bpm",
    10109: "Heartbeat 90 bpm",
    10110: "Heartbeat 105 bpm",
    10111: "Air Conditioner",
    10112: "Airplane",
    10118: "Summer Ocean",
    10119: "Beach",
    10120: "Ocean",
    10121: "Meditation",
    10122: "Loud Fan",
    10123: "Soothing Fan",
}

RESTORE_ALL_SOUNDS: dict[int, str] = {**RESTORE_HIDDEN_SOUNDS, **RESTORE_ALARM_SOUNDS, **RESTORE_SLEEP_SOUNDS}


def sound_title(sound_id: int, catalog: dict[int, str] | None = None) -> str | None:
    """Human name for a ``sound.id`` from the live catalog first, then the built-in tables."""
    if catalog and sound_id in catalog:
        return catalog[sound_id]
    return RESTORE_ALL_SOUNDS.get(sound_id)
