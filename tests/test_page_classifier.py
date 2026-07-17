from __future__ import annotations

from app.classifiers.page_classifier import PageContext, classify_page, classify_price_visibility
from app.schemas.product_observation import PageClassification, PriceVisibility


def context(text: str, status: int | None = 200, succeeded: bool = True, title: str = "") -> PageContext:
    return PageContext(
        navigation_succeeded=succeeded,
        http_status=status,
        final_url="https://www.partzilla.com/product/kawasaki/41080-1514",
        page_title=title,
        visible_text=text,
        requested_part_number="41080-1514",
    )


def test_classifies_normal_product_page() -> None:
    result = classify_page(
        context(
            "KAWASAKI OEM DISC 41080-1514 MSRP: $282.32 Manufacturer: KAWASAKI Quantity Ships in 3 to 4 days",
            title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
        )
    )

    assert result.classification == PageClassification.NORMAL_PRODUCT


def test_duplicate_classification_evidence_is_removed() -> None:
    result = classify_page(
        context(
            "KAWASAKI OEM DISC 41080-1514 MSRP: $282.32 Manufacturer: KAWASAKI Quantity Ships in 3 to 4 days",
            title="KAWASAKI OEM DISC - 41080-1514 | partzilla.com",
        )
    )

    assert result.evidence.count("HTTP 200") == 1


def test_classifies_normal_product_with_sign_in_required_price() -> None:
    result = classify_page(context("41080-1514 MSRP: $282.32 Manufacturer: KAWASAKI Sign In To See Price Ships in 3 to 4 days"))
    price = classify_price_visibility("MSRP: $282.32\nSign In To See Price\nShips in 3 to 4 days")

    assert result.classification == PageClassification.NORMAL_PRODUCT
    assert price.visibility == PriceVisibility.SIGN_IN_REQUIRED


def test_classifies_blocked_page() -> None:
    result = classify_page(context("Access denied. Automated request blocked.", status=403))

    assert result.classification == PageClassification.BLOCKED


def test_classifies_challenge_page() -> None:
    result = classify_page(context("Please verify you are human before continuing."))

    assert result.classification == PageClassification.CHALLENGE


def test_classifies_not_found_page() -> None:
    result = classify_page(context("Product not found", status=404))

    assert result.classification == PageClassification.NOT_FOUND


def test_classifies_navigation_error() -> None:
    result = classify_page(
        PageContext(
            navigation_succeeded=False,
            http_status=None,
            final_url=None,
            page_title=None,
            visible_text="",
            exception_message="Timeout",
        )
    )

    assert result.classification == PageClassification.NAVIGATION_ERROR


def test_classifies_unknown_page() -> None:
    result = classify_page(context("Welcome to a generic page with no useful product details.", status=200))

    assert result.classification == PageClassification.UNKNOWN


def test_embedded_script_text_does_not_cause_false_blocked_classification() -> None:
    result = classify_page(context("KAWASAKI OEM DISC 41080-1514 MSRP: $282.32 Manufacturer: KAWASAKI Ships in 3 to 4 days"))

    assert result.classification == PageClassification.NORMAL_PRODUCT


def test_embedded_script_text_does_not_cause_false_captcha_classification() -> None:
    result = classify_page(context("KAWASAKI OEM DISC 41080-1514 MSRP: $282.32 Manufacturer: KAWASAKI Sign In To See Price"))

    assert result.classification == PageClassification.NORMAL_PRODUCT


def test_price_visibility_visible_selling_price() -> None:
    result = classify_price_visibility("Product Price\n$199.99\nMSRP: $282.32")

    assert result.visibility == PriceVisibility.VISIBLE


def test_msrp_alone_is_not_mistaken_for_selling_price() -> None:
    result = classify_price_visibility("MSRP: $282.32")

    assert result.visibility == PriceVisibility.NOT_PRESENT


def test_no_price_information_is_not_present() -> None:
    result = classify_price_visibility("KAWASAKI OEM DISC 41080-1514")

    assert result.visibility == PriceVisibility.NOT_PRESENT


def test_recommended_product_price_is_not_main_product_selling_price() -> None:
    result = classify_price_visibility(
        "\n".join(
            [
                "KAWASAKI OEM DISC 41080-1514",
                "MSRP: $282.32",
                "Riders Also Bought",
                "Price",
                "$89.99",
            ]
        )
    )

    assert result.visibility == PriceVisibility.NOT_PRESENT


def test_cross_sell_price_is_not_main_product_selling_price() -> None:
    result = classify_price_visibility(
        "\n".join(
            [
                "KAWASAKI OEM DISC 41080-1514",
                "Partzilla Picks",
                "Price",
                "$12.99",
            ]
        )
    )

    assert result.visibility == PriceVisibility.NOT_PRESENT
