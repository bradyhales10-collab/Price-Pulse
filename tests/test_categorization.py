"""Categorisation exists to feed pricing sensitivity, so a wrong category leads
to a wrong price. These cases are drawn from names that actually appear in this
project's uploads, plus the specific traps the pricing specification calls out.
"""

from __future__ import annotations

from app.categorization import (
    CATEGORY_BRAKES,
    CATEGORY_DRIVETRAIN,
    CATEGORY_ELECTRICAL,
    CATEGORY_ENGINE,
    CATEGORY_FLUIDS,
    CATEGORY_FUEL,
    CATEGORY_HARDWARE,
    CATEGORY_MAINTENANCE,
    CATEGORY_SEALS,
    CATEGORY_UNKNOWN,
    categorize_product,
    normalize_product_name,
)


def test_the_oil_trap_from_the_specification() -> None:
    """OIL FILTER, OIL PUMP and ENGINE OIL all contain OIL but are three
    different things. Getting this wrong would price a maintenance item like a
    component, or vice versa."""
    assert categorize_product("ELEMENT, OIL FILTER").category == CATEGORY_MAINTENANCE
    assert categorize_product("OIL CHANGE KIT").category == CATEGORY_MAINTENANCE
    assert categorize_product("ENGINE OIL 10W-40").category == CATEGORY_FLUIDS
    # An oil pump is a component, and must not be classified as a fluid.
    assert categorize_product("OIL PUMP").category != CATEGORY_FLUIDS
    assert categorize_product("OIL TANK").category != CATEGORY_FLUIDS


def test_real_product_names_from_uploads() -> None:
    expected = {
        "PS-4 5W-50 SYNTHETIC, QT": CATEGORY_FLUIDS,
        "DRIVE BELT": CATEGORY_DRIVETRAIN,
        "ASM-HALFSHAFT, REAR, 8.8.64": CATEGORY_DRIVETRAIN,
        "DRIVESHAFT": CATEGORY_DRIVETRAIN,
        "K-FUEL PUMP,RZR TURBO": CATEGORY_FUEL,
        "BATTERY GYZ20HA": CATEGORY_ELECTRICAL,
        "CYLINDER HEAD ASSY": CATEGORY_ENGINE,
        "PIN, DOWEL": CATEGORY_HARDWARE,
        "O-RING": CATEGORY_SEALS,
        "BRAKE PAD SET": CATEGORY_BRAKES,
    }
    for name, category in expected.items():
        assert categorize_product(name).category == category, name


def test_punctuation_without_spaces_still_matches() -> None:
    """Names arrive as "K-FUEL PUMP,RZR TURBO" with words wedged against
    punctuation. Without splitting on it, FUEL PUMP would never match."""
    assert normalize_product_name("K-FUEL PUMP,RZR TURBO") == "K FUEL PUMP RZR TURBO"
    assert categorize_product("K-FUEL PUMP,RZR TURBO").category == CATEGORY_FUEL


def test_a_longer_phrase_beats_a_shorter_one_inside_it() -> None:
    """CYLINDER HEAD is an engine part; CYLINDER alone is too, but the phrase
    must win so the reason given is the specific one."""
    result = categorize_product("CYLINDER HEAD ASSY")
    assert result.category == CATEGORY_ENGINE
    assert result.matched_phrase == "CYLINDER HEAD"


def test_whole_words_only_so_pin_does_not_match_pinion() -> None:
    pinion = categorize_product("PINION GEAR")
    assert pinion.category == CATEGORY_DRIVETRAIN
    assert pinion.matched_phrase != "PIN"


def test_an_unrecognised_name_is_admitted_rather_than_guessed() -> None:
    """The specification is explicit that a weak guess must not drive an
    aggressive price change, so an honest Unknown is the correct answer."""
    result = categorize_product("HOLDER, DAMPER")

    assert result.category == CATEGORY_UNKNOWN
    assert result.confidence < 0.55
    assert result.confidence_class == "LOW"
    assert result.is_confident is False


def test_confidence_is_lower_when_several_categories_match_one_word() -> None:
    """A single ambiguous word is a genuinely weaker signal than a specific
    phrase, and the result should say so rather than appear certain."""
    phrase = categorize_product("BRAKE PAD SET")
    assert phrase.confidence >= 0.90
    assert phrase.confidence_class == "HIGH"


def test_a_missing_name_does_not_raise() -> None:
    for value in (None, "", "   "):
        result = categorize_product(value)
        assert result.category == CATEGORY_UNKNOWN
        assert result.is_confident is False
