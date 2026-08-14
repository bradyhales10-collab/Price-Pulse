"""Work out what kind of part something is from its name.

Uploaded files rarely carry a usable category, so this derives one. It exists
to feed pricing sensitivity: a high-volume oil filter and an obscure bracket
should not be priced the same way.

The hard part is that a keyword alone is misleading. "OIL PUMP" is an engine
component, "OIL FILTER" is maintenance, and "ENGINE OIL" is a fluid, yet all
three contain OIL. Longer, more specific phrases are therefore matched first
and win outright, and a few phrases explicitly block a category they would
otherwise fall into.

Every result carries a confidence and a plain reason, because a weak guess must
not be allowed to drive an aggressive price change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORY_MAINTENANCE = "Maintenance / Filters"
CATEGORY_FLUIDS = "Fluids / Lubricants"
CATEGORY_DRIVETRAIN = "Drivetrain / Transmission"
CATEGORY_BRAKES = "Brakes"
CATEGORY_ELECTRICAL = "Electrical / Ignition"
CATEGORY_ENGINE = "Engine Internal"
CATEGORY_FUEL = "Fuel / Intake"
CATEGORY_COOLING = "Cooling"
CATEGORY_SUSPENSION = "Suspension / Steering"
CATEGORY_WHEELS = "Wheels / Hubs"
CATEGORY_BODY = "Body / Plastics / Trim"
CATEGORY_EXHAUST = "Exhaust"
CATEGORY_HARDWARE = "Hardware / Fasteners"
CATEGORY_SEALS = "Gaskets / Seals / O-Rings"
CATEGORY_UNKNOWN = "Other / Unknown"

HIGH_CONFIDENCE = 0.80
MEDIUM_CONFIDENCE = 0.55

# Phrases are matched longest-first, so a more specific phrase always beats a
# shorter one that happens to be contained in the same name.
CATEGORY_PHRASES: dict[str, tuple[str, ...]] = {
    CATEGORY_MAINTENANCE: (
        "OIL FILTER", "FILTER OIL", "OIL CLEANER", "AIR FILTER", "FUEL FILTER",
        "FILTER ELEMENT", "ELEMENT OIL FILTER", "OIL CHANGE KIT", "SERVICE KIT",
        "MAINTENANCE KIT", "SPARK PLUG",
    ),
    CATEGORY_FLUIDS: (
        "BRAKE FLUID", "GEAR OIL", "TRANSMISSION FLUID", "ENGINE OIL", "SYNTHETIC",
        "COOLANT", "ANTIFREEZE", "LUBRICANT", "GREASE", "LUBE",
    ),
    CATEGORY_DRIVETRAIN: (
        "DRIVE BELT", "CVT BELT", "BELT DRIVE", "DRIVE SHAFT", "DRIVESHAFT",
        "HALFSHAFT", "HALF SHAFT", "PROP SHAFT", "CLUTCH", "VARIATOR", "GEARCASE",
        "DIFFERENTIAL", "SPROCKET", "TRANSMISSION", "PINION", "AXLE", "CHAIN",
    ),
    CATEGORY_BRAKES: (
        "BRAKE PAD", "BRAKE SHOE", "BRAKE DISC", "BRAKE DISK", "BRAKE ROTOR",
        "BRAKE CALIPER", "MASTER CYLINDER", "CALIPER", "ROTOR",
    ),
    CATEGORY_ELECTRICAL: (
        "BATTERY", "STARTER", "STATOR", "REGULATOR", "RECTIFIER", "IGNITION",
        "SOLENOID", "ECU", "ECM", "CDI", "SENSOR", "RELAY", "HARNESS",
        "SWITCH", "COIL", "WIRE",
    ),
    CATEGORY_ENGINE: (
        "CYLINDER HEAD", "CONNECTING ROD", "CAM CHAIN", "CRANKSHAFT", "CAMSHAFT",
        "PISTON", "ROCKER", "TENSIONER", "VALVE", "CYLINDER",
    ),
    CATEGORY_FUEL: (
        "FUEL PUMP", "FUEL TANK", "FUEL INJECTOR", "INJECTOR", "CARBURETOR",
        "THROTTLE", "PETCOCK", "INTAKE", "CARB",
    ),
    CATEGORY_COOLING: (
        "WATER PUMP", "COOLING FAN", "COOLANT HOSE", "RADIATOR", "THERMOSTAT",
    ),
    CATEGORY_SUSPENSION: (
        "CONTROL ARM", "BALL JOINT", "TIE ROD", "A-ARM", "A ARM", "SHOCK",
        "STEERING", "STRUT", "FORK",
    ),
    CATEGORY_WHEELS: ("WHEEL BEARING", "WHEEL", "RIM", "HUB"),
    CATEGORY_BODY: (
        "WINDSHIELD", "FAIRING", "FENDER", "BUMPER", "PANEL", "PLASTIC",
        "SEAT", "ROOF", "DOOR", "COVER",
    ),
    CATEGORY_EXHAUST: ("EXHAUST", "MUFFLER", "HEADER", "EXHAUST PIPE"),
    CATEGORY_SEALS: ("O-RING", "ORING", "O RING", "GASKET", "SEAL", "PACKING"),
    CATEGORY_HARDWARE: (
        "DRAIN WASHER", "RETAINER", "GROMMET", "BRACKET", "SPACER", "WASHER",
        "COLLAR", "RIVET", "SCREW", "BOLT", "CLIP", "NUT", "PIN", "DOWEL",
    ),
}

# Some phrases would otherwise be captured by a broader category. A name
# containing one of these is barred from that category outright, which is what
# keeps "OIL PUMP" out of Fluids and "BEARING" out of Wheels.
CATEGORY_BLOCKERS: dict[str, tuple[str, ...]] = {
    CATEGORY_FLUIDS: ("OIL PUMP", "OIL TANK", "OIL COOLER", "OIL FILTER", "OIL SEAL", "OIL LINE", "OIL PAN"),
    CATEGORY_WHEELS: ("BALL BEARING", "ENGINE BEARING", "ROD BEARING"),
    CATEGORY_BRAKES: ("CLUTCH MASTER CYLINDER",),
}

_SEPARATORS = re.compile(r"[,/\\\-_()\[\]]+")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class CategoryResult:
    category: str
    confidence: float
    reason: str
    matched_phrase: str | None = None
    alternatives: list[str] = field(default_factory=list)

    @property
    def confidence_class(self) -> str:
        if self.confidence >= HIGH_CONFIDENCE:
            return "HIGH"
        if self.confidence >= MEDIUM_CONFIDENCE:
            return "MEDIUM"
        return "LOW"

    @property
    def is_confident(self) -> bool:
        """Whether this is solid enough to justify an aggressive price move."""
        return self.confidence >= HIGH_CONFIDENCE


def normalize_product_name(name: str | None) -> str:
    """Uppercase, and turn punctuation into spaces.

    Real names arrive as "ASM-HALFSHAFT, REAR, 8.8.64" and "K-FUEL PUMP,RZR
    TURBO", where the useful words are wedged against punctuation with no
    spaces. Splitting on it is what lets those match at all.
    """
    if not name:
        return ""
    upper = str(name).upper()
    spaced = _SEPARATORS.sub(" ", upper)
    return _SPACES.sub(" ", spaced).strip()


def categorize_product(name: str | None) -> CategoryResult:
    normalized = normalize_product_name(name)
    if not normalized:
        return CategoryResult(CATEGORY_UNKNOWN, 0.0, "No product name to classify")

    padded = f" {normalized} "
    matches: list[tuple[int, str, str]] = []

    for category, phrases in CATEGORY_PHRASES.items():
        if any(blocker in normalized for blocker in CATEGORY_BLOCKERS.get(category, ())):
            continue
        for phrase in phrases:
            # Whole-word matching, so PIN does not match PINION and CARB does
            # not match CARBURETOR's category twice.
            if f" {phrase} " in padded:
                matches.append((len(phrase), category, phrase))

    if not matches:
        return CategoryResult(
            CATEGORY_UNKNOWN, 0.35, "No strong category keyword match", alternatives=[]
        )

    # Longest phrase wins: it is the most specific statement about the part.
    matches.sort(key=lambda item: item[0], reverse=True)
    best_length, best_category, best_phrase = matches[0]
    other_categories = [category for _, category, _ in matches if category != best_category]

    if len(best_phrase.split()) >= 2:
        confidence = 0.95
        reason = f"Matched phrase {best_phrase}"
    elif not other_categories:
        confidence = 0.85
        reason = f"Matched keyword {best_phrase}"
    else:
        # A single word matching several categories is a genuinely weaker
        # signal, so it must not be reported as though it were certain.
        confidence = 0.60
        reason = f"Matched keyword {best_phrase}, but {', '.join(sorted(set(other_categories)))} also matched"

    return CategoryResult(
        category=best_category,
        confidence=confidence,
        reason=reason,
        matched_phrase=best_phrase,
        alternatives=sorted(set(other_categories)),
    )
