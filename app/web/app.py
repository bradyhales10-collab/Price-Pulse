from __future__ import annotations

import logging
import math
from datetime import UTC
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth_session import (
    InvalidAuthStateError,
    auth_state_exists,
    auth_state_status,
    delete_auth_state,
    save_uploaded_auth_state,
)
from app.collection_jobs import (
    claim_next_local_job,
    claim_next_local_login_refresh,
    complete_local_job,
    current_active_job,
    job_status,
    latest_job_for_import,
    local_agent_status,
    local_job_input_path,
    plan_import_collection,
    plan_ui_collection,
    queue_local_collection_job,
    queue_local_login_refresh,
    register_local_agent,
    start_price_collection_job,
    update_local_job_progress,
    validate_collection_request,
)
from app.collector_bridge import import_collection_summary, selected_parts_csv
from app.comparison import ComparisonFilters
from app.competitors.registry import list_competitors, select_competitors, short_display_name
from app.config import OUTPUT_DIR
from app.exports.review_export import export_review
from app.imports import (
    ALL_FIELDS,
    IMPORT_DIR,
    REQUIRED_FIELDS,
    clear_import_history,
    confirm_import,
    delete_import_batch,
    import_history,
    preview_import,
    read_headers,
    save_upload,
    validation_errors_csv,
    workbook_sheets,
    write_import_template,
)
from app.maintenance import (
    clear_comparison_results,
    clear_pending_review_queue,
    clear_scan_runs,
    reset_all_test_data,
)
from app.management import management_summary
from app.manufacturer_registry import manufacturer_coverage_settings, save_manufacturer_coverage_settings
from app.pricing_rules import (
    apply_pricing_rule_preset,
    list_manufacturer_rule_overrides,
    list_pricing_rule_presets,
    list_pricing_rules,
    update_manufacturer_rule_override,
    update_pricing_rule,
)
from app.reviews import (
    ALL_BUCKETS,
    ALL_STATUSES,
    PENDING_REVIEW,
    REVIEW_BUCKETS,
    REVIEW_STATUSES,
    comparison_review_rows,
    review_queue,
    review_rows,
    save_bulk_review_decision,
    save_review_decision,
    suggested_price_for_product,
    undo_saved_review_decision,
)
from app.web.formatters import format_timestamp, humanize_status
from app.web.queries import (
    DEFAULT_PAGE_SIZE,
    PAGE_SIZE_OPTIONS,
    CatalogFilters,
    DashboardDatabaseError,
    catalog_data,
    dashboard_data,
    import_batch_label,
    manufacturer_options,
    product_detail,
    quality_data,
    scan_run_detail,
    scan_runs,
    short_competitor_name,
)

LOGGER = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["datetime"] = format_timestamp
templates.env.filters["humanize"] = humanize_status
templates.env.filters["price"] = lambda value: _format_price(value)
templates.env.filters["short_competitor"] = short_competitor_name
templates.env.globals["page_size_options"] = PAGE_SIZE_OPTIONS
templates.env.globals["competitor_columns"] = [
    {"key": adapter.competitor_key, "short_name": short_display_name(adapter), "display_name": adapter.display_name}
    for adapter in list_competitors()
]


def create_app(database: Path) -> FastAPI:
    app = FastAPI(title="Part Pulse")
    app.state.database = database
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.exception_handler(DashboardDatabaseError)
    async def database_error(request: Request, exc: DashboardDatabaseError):
        LOGGER.exception("Dashboard database error: %s", exc)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc), "database": app.state.database, "active": ""},
            status_code=503,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        LOGGER.exception("Unexpected dashboard error")
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "The dashboard could not load this page.", "database": app.state.database, "active": ""},
            status_code=500,
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"active": "dashboard", "database": app.state.database, "management": management_summary(app.state.database), **dashboard_data(app.state.database)},
        )

    @app.post("/data/reset")
    async def data_reset(request: Request):
        body = (await request.body()).decode("utf-8", errors="replace")
        confirmation = parse_qs(body, keep_blank_values=True).get("confirmation", [""])[-1].strip()
        if confirmation != "CLEAR DATA":
            return RedirectResponse("/settings?message=Data%20was%20not%20cleared.%20Type%20CLEAR%20DATA%20to%20confirm.", status_code=303)
        reset_all_test_data(app.state.database)
        return RedirectResponse("/settings?message=All%20pricing%20data%20was%20cleared.", status_code=303)

    @app.get("/price-check")
    def price_check_shortcut():
        return RedirectResponse("/imports", status_code=303)

    @app.get("/products", response_class=HTMLResponse)
    def products(
        request: Request,
        search: str = "",
        manufacturer: str = "",
        price_type: str = "",
        availability: str = "",
        superseded: str = "",
        confidence: str = "",
        needs_review: int = 0,
        sort: str = "last_checked",
        page: int = 1,
        page_size: int = 50,
    ):
        data = catalog_data(
            app.state.database,
            CatalogFilters(
                search=search,
                manufacturer=manufacturer,
                price_type=price_type,
                availability=availability,
                superseded=superseded,
                confidence=confidence,
                needs_review=bool(needs_review),
                sort=sort,
                page=page,
                page_size=page_size,
            ),
        )
        return templates.TemplateResponse(
            request,
            "products.html",
            {"active": "products", "database": app.state.database, **data},
        )

    @app.get("/products/{product_id}", response_class=HTMLResponse)
    def product(request: Request, product_id: int, back: str = ""):
        data = product_detail(app.state.database, product_id)
        if data is None:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": "Product not found.", "database": app.state.database, "active": "products"},
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "product_detail.html",
            {
                "active": "products",
                "database": app.state.database,
                "back_url": _safe_back_url(back),
                **data,
            },
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        return templates.TemplateResponse(
            request,
            "runs.html",
            {"active": "runs", "database": app.state.database, "runs": scan_runs(app.state.database)},
        )

    @app.post("/runs/clear")
    def runs_clear():
        clear_scan_runs(app.state.database)
        return RedirectResponse("/runs", status_code=303)

    @app.get("/comparison", response_class=HTMLResponse)
    def comparison(
        request: Request,
        search: str = "",
        manufacturer: str = "",
        price_position: str = "",
        competitor_discounted: int = 0,
        scan_priority: str = "",
        missing_competitor_price: int = 0,
        hidden_competitor_price: int = 0,
        needs_review: int = 0,
        import_batch_id: str | None = None,
        page: int = 1,
        page_size: int = 50,
        message: str = "",
    ):
        page_size = page_size if page_size in PAGE_SIZE_OPTIONS else DEFAULT_PAGE_SIZE
        page = max(1, page)
        parsed_import_batch_id = _optional_int_form_value(import_batch_id)
        filters = ComparisonFilters(
            search=search,
            manufacturer=manufacturer,
            price_position=price_position,
            competitor_discounted=bool(competitor_discounted),
            scan_priority=scan_priority,
            missing_competitor_price=bool(missing_competitor_price),
            hidden_competitor_price=bool(hidden_competitor_price),
            needs_review=bool(needs_review),
            import_batch_id=parsed_import_batch_id,
        )
        rows = comparison_review_rows(app.state.database, filters)
        total = len(rows)
        summary = {
            "our_price_higher": sum(1 for row in rows if row.get("price_difference_cents") is not None and row["price_difference_cents"] > 0),
            "our_price_lower": sum(1 for row in rows if row.get("price_difference_cents") is not None and row["price_difference_cents"] < 0),
            "missing_competitor_price": sum(1 for row in rows if not row.get("lowest_competitor_name")),
            "hidden_competitor_price": sum(1 for row in rows if row.get("motosport_hidden_price")),
            "needs_review": sum(1 for row in rows if not row.get("saved_to_catalog")),
        }
        total_pages = max(1, math.ceil(total / page_size))
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        visible_rows = rows[offset : offset + page_size]
        page_query = _comparison_page_query(filters, page_size)
        return templates.TemplateResponse(
            request,
            "comparison.html",
            {
                "active": "comparison",
                "database": app.state.database,
                "rows": visible_rows,
                "filters": filters,
                "total": total,
                "summary": summary,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "message": message,
                "statuses": REVIEW_STATUSES,
                "page_query": page_query,
                "quick_filter_queries": _comparison_quick_filter_queries(filters, page_size),
                "manufacturers": manufacturer_options(app.state.database),
                "selected_import": import_batch_label(app.state.database, parsed_import_batch_id),
            },
        )

    @app.post("/comparison/clear")
    def comparison_clear():
        clear_comparison_results(app.state.database)
        return RedirectResponse("/imports", status_code=303)

    @app.post("/comparison/export")
    async def comparison_export(request: Request):
        form = await _urlencoded_form(request)
        selected = form.get("selected", "")
        scope = form.get("scope", "all")
        export_filters = ComparisonFilters(import_batch_id=_optional_int_form_value(form.get("import_batch_id")))
        if scope == "selected" and form.get("all_matching") == "1":
            query = parse_qs(str(form.get("query", "")).lstrip("?"), keep_blank_values=True)
            export_filters = ComparisonFilters(
                search=query.get("search", [""])[-1],
                manufacturer=query.get("manufacturer", [""])[-1],
                price_position=query.get("price_position", [""])[-1],
                competitor_discounted=query.get("competitor_discounted", [""])[-1] == "1",
                scan_priority=query.get("scan_priority", [""])[-1],
                missing_competitor_price=query.get("missing_competitor_price", [""])[-1] == "1",
                hidden_competitor_price=query.get("hidden_competitor_price", [""])[-1] == "1",
                needs_review=query.get("needs_review", [""])[-1] == "1",
                import_batch_id=_optional_int_form_value(query.get("import_batch_id", [""])[-1]),
            )
        rows = comparison_review_rows(app.state.database, export_filters)
        if scope == "selected" and form.get("all_matching") != "1" and selected.strip():
            ids = {int(value) for value in selected.split(",") if value.strip().isdigit()}
            rows = [row for row in rows if row["product_id"] in ids]
        path = export_review(rows, OUTPUT_DIR / "exports")
        return FileResponse(path, filename=path.name)

    @app.post("/comparison/{product_id}/review")
    async def comparison_review_save(request: Request, product_id: int):
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        selected_rule_codes = parsed.get("rule_code", [])
        suggested_new_price = form.get("suggested_new_price", "")
        displayed_suggested_new_price = form.get("displayed_suggested_new_price", "")
        try:
            if form.get("use_rule_suggestion") == "1" and (
                not suggested_new_price.strip() or suggested_new_price.strip() == displayed_suggested_new_price.strip()
            ):
                suggestion = suggested_price_for_product(app.state.database, product_id, selected_rule_codes)
                suggested_new_price = suggestion["suggested_price"]
            save_review_decision(
                app.state.database,
                product_id=product_id,
                review_status=form.get("review_status", "Approved"),
                suggested_new_price=suggested_new_price,
                notes=form.get("notes", ""),
                reviewer=form.get("reviewer", ""),
                applied_rule_codes=selected_rule_codes,
            )
        except ValueError as exc:
            return RedirectResponse(f"/imports?message={quote(str(exc))}", status_code=303)
        query = form.get("return_query", "")
        suffix = f"&message={quote('Saved updated price.')}" if query else f"?message={quote('Saved updated price.')}"
        return RedirectResponse(f"/imports?{query}{suffix}" if query else f"/imports{suffix}", status_code=303)

    @app.post("/comparison/{product_id}/undo")
    async def comparison_review_undo(request: Request, product_id: int):
        form = await _urlencoded_form(request)
        try:
            undo_saved_review_decision(app.state.database, product_id=product_id)
        except ValueError as exc:
            message = quote(str(exc))
        else:
            message = quote("Restored the catalog price and reopened this row for review.")
        query = form.get("return_query", "")
        suffix = f"&message={message}" if query else f"?message={message}"
        return RedirectResponse(f"/imports?{query}{suffix}" if query else f"/imports{suffix}", status_code=303)

    @app.post("/comparison/bulk-save")
    async def comparison_bulk_save(request: Request):
        payload = await request.json()
        payload = payload if isinstance(payload, dict) else {}
        rows = payload.get("rows", [])
        if payload.get("all_matching"):
            query = parse_qs(str(payload.get("query", "")).lstrip("?"), keep_blank_values=True)
            filters = ComparisonFilters(
                search=query.get("search", [""])[-1],
                manufacturer=query.get("manufacturer", [""])[-1],
                price_position=query.get("price_position", [""])[-1],
                competitor_discounted=query.get("competitor_discounted", [""])[-1] == "1",
                scan_priority=query.get("scan_priority", [""])[-1],
                missing_competitor_price=query.get("missing_competitor_price", [""])[-1] == "1",
                hidden_competitor_price=query.get("hidden_competitor_price", [""])[-1] == "1",
                needs_review=query.get("needs_review", [""])[-1] == "1",
                import_batch_id=_optional_int_form_value(query.get("import_batch_id", [""])[-1]),
            )
            visible_overrides = {str(item.get("product_id")): item for item in rows if isinstance(item, dict)}
            rows = [
                {
                    "product_id": row["product_id"],
                    "suggested_new_price": visible_overrides.get(str(row["product_id"]), {}).get("suggested_new_price", row.get("suggested_new_price", "")),
                    "review_status": "Approved",
                    "rule_codes": visible_overrides.get(str(row["product_id"]), {}).get("rule_codes", []),
                }
                for row in comparison_review_rows(app.state.database, filters)
            ]
        saved = 0
        errors: list[str] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            try:
                product_id = int(item.get("product_id"))
                price = str(item.get("suggested_new_price") or "").strip()
                if not price:
                    continue
                save_review_decision(
                    app.state.database,
                    product_id=product_id,
                    review_status=str(item.get("review_status") or "Approved"),
                    suggested_new_price=price,
                    applied_rule_codes=[str(code) for code in item.get("rule_codes", []) if str(code).strip()],
                )
                saved += 1
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))
        return JSONResponse({"saved": saved, "errors": errors})

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request, status: str = PENDING_REVIEW, bucket: str = ALL_BUCKETS, page: int = 1, page_size: int = 50, message: str = ""):
        queue = review_queue(app.state.database, status=status, bucket=bucket, page=page, page_size=page_size)
        return templates.TemplateResponse(
            request,
            "reviews.html",
            {
                "active": "reviews",
                "database": app.state.database,
                "queue": queue,
                "statuses": REVIEW_STATUSES,
                "all_statuses": (ALL_STATUSES, *REVIEW_STATUSES),
                "buckets": REVIEW_BUCKETS,
                "error": "",
                "message": message,
            },
        )

    @app.post("/reviews/clear")
    def reviews_clear():
        clear_pending_review_queue(app.state.database)
        return RedirectResponse("/reviews", status_code=303)

    @app.post("/reviews/bulk")
    async def review_bulk_save(request: Request):
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        product_ids = [int(value) for value in parsed.get("selected", []) if value.isdigit()]
        status = (parsed.get("review_status", [PENDING_REVIEW])[-1] or PENDING_REVIEW)
        notes = parsed.get("notes", [""])[-1]
        return_status = parsed.get("return_status", [PENDING_REVIEW])[-1]
        return_bucket = parsed.get("return_bucket", [ALL_BUCKETS])[-1]
        page_size = parsed.get("page_size", ["50"])[-1]
        try:
            saved = save_bulk_review_decision(app.state.database, product_ids=product_ids, review_status=status, notes=notes)
        except ValueError as exc:
            queue = review_queue(app.state.database, status=return_status, bucket=return_bucket, page_size=_int_form_value(page_size, 50))
            return templates.TemplateResponse(
                request,
                "reviews.html",
                {
                    "active": "reviews",
                    "database": app.state.database,
                    "queue": queue,
                    "statuses": REVIEW_STATUSES,
                    "all_statuses": (ALL_STATUSES, *REVIEW_STATUSES),
                    "buckets": REVIEW_BUCKETS,
                    "error": str(exc),
                    "message": "",
                },
                status_code=400,
            )
        message = quote(f"Updated {saved} rows.")
        return RedirectResponse(f"/reviews?status={quote(return_status)}&bucket={quote(return_bucket)}&page_size={page_size}&message={message}", status_code=303)

    @app.post("/reviews/{product_id}")
    async def review_save(request: Request, product_id: int):
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        status = form.get("review_status", PENDING_REVIEW)
        page = form.get("page", "1")
        page_size = form.get("page_size", "50")
        return_bucket = form.get("return_bucket", ALL_BUCKETS)
        return_status = form.get("return_status", status)
        selected_rule_codes = parsed.get("rule_code", [])
        suggested_new_price = form.get("suggested_new_price", "")
        displayed_suggested_new_price = form.get("displayed_suggested_new_price", "")
        try:
            if form.get("use_rule_suggestion") == "1" and (
                not suggested_new_price.strip() or suggested_new_price.strip() == displayed_suggested_new_price.strip()
            ):
                suggestion = suggested_price_for_product(app.state.database, product_id, selected_rule_codes)
                suggested_new_price = suggestion["suggested_price"]
            save_review_decision(
                app.state.database,
                product_id=product_id,
                review_status=status,
                suggested_new_price=suggested_new_price,
                notes=form.get("notes", ""),
                reviewer=form.get("reviewer", ""),
                applied_rule_codes=selected_rule_codes,
            )
        except ValueError as exc:
            queue = review_queue(app.state.database, status=return_status, bucket=return_bucket, page=_int_form_value(page, 1), page_size=_int_form_value(page_size, 50))
            return templates.TemplateResponse(
                request,
                "reviews.html",
                {
                    "active": "reviews",
                    "database": app.state.database,
                    "queue": queue,
                    "statuses": REVIEW_STATUSES,
                    "all_statuses": (ALL_STATUSES, *REVIEW_STATUSES),
                    "buckets": REVIEW_BUCKETS,
                    "error": str(exc),
                    "message": "",
                },
                status_code=400,
            )
        return RedirectResponse(f"/reviews?status={quote(return_status)}&bucket={quote(return_bucket)}&page={page}&page_size={page_size}", status_code=303)

    @app.get("/reviews/export")
    def reviews_export(status: str = ALL_STATUSES, bucket: str = ALL_BUCKETS):
        rows = review_rows(app.state.database, status=status, bucket=bucket)
        path = export_review(rows, OUTPUT_DIR / "exports")
        return FileResponse(path, filename=path.name)

    @app.get("/rules", response_class=HTMLResponse)
    def rules(request: Request, message: str = "", error: str = ""):
        return templates.TemplateResponse(
            request,
            "rules.html",
            {
                "active": "rules",
                "database": app.state.database,
                "rules": list_pricing_rules(app.state.database),
                "manufacturer_overrides": list_manufacturer_rule_overrides(app.state.database),
                "presets": list_pricing_rule_presets(),
                "message": message,
                "error": error,
            },
        )

    @app.post("/rules/presets/{preset_code}", response_class=HTMLResponse)
    def rule_preset_update(request: Request, preset_code: str):
        try:
            apply_pricing_rule_preset(app.state.database, preset_code)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "rules.html",
                {
                    "active": "rules",
                    "database": app.state.database,
                    "rules": list_pricing_rules(app.state.database),
                    "manufacturer_overrides": list_manufacturer_rule_overrides(app.state.database),
                    "presets": list_pricing_rule_presets(),
                    "message": "",
                    "error": str(exc),
                },
                status_code=400,
            )
        return RedirectResponse("/rules?message=Preset%20applied", status_code=303)

    @app.post("/rules/{rule_code}", response_class=HTMLResponse)
    async def rule_update(request: Request, rule_code: str):
        form = await _urlencoded_form(request)
        try:
            update_pricing_rule(
                app.state.database,
                rule_code=rule_code,
                is_enabled=form.get("is_enabled") == "1",
                setting_value=form.get("setting_value", ""),
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "rules.html",
                {
                    "active": "rules",
                    "database": app.state.database,
                    "rules": list_pricing_rules(app.state.database),
                    "manufacturer_overrides": list_manufacturer_rule_overrides(app.state.database),
                    "presets": list_pricing_rule_presets(),
                    "message": "",
                    "error": str(exc),
                },
                status_code=400,
            )
        return RedirectResponse("/rules?message=Rule%20saved", status_code=303)

    @app.post("/rules/manufacturers/{manufacturer}", response_class=HTMLResponse)
    async def manufacturer_rule_override_update(request: Request, manufacturer: str):
        form = await _urlencoded_form(request)
        try:
            update_manufacturer_rule_override(
                app.state.database,
                manufacturer=manufacturer,
                is_enabled=form.get("is_enabled") == "1",
                adjustment_cents=form.get("adjustment_cents", "0"),
                ending_cents=form.get("ending_cents", "99"),
                minimum_margin_pct=form.get("minimum_margin_pct", "20"),
            )
        except ValueError as exc:
            return RedirectResponse(f"/rules?error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/rules?message=OEM%20rule%20override%20saved", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, message: str = ""):
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "active": "settings",
                "database": app.state.database,
                "coverage": manufacturer_coverage_settings(),
                "competitors": list_competitors(),
                "message": message,
            },
        )

    @app.post("/settings/coverage")
    async def settings_coverage_save(request: Request):
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        selected = {
            competitor.competitor_key: parsed.get(f"coverage_{competitor.competitor_key}", [])
            for competitor in list_competitors()
        }
        save_manufacturer_coverage_settings(selected)
        return RedirectResponse("/settings?message=Coverage%20saved", status_code=303)

    @app.get("/sessions", response_class=HTMLResponse)
    def login_sessions(request: Request, message: str = "", error: str = ""):
        return templates.TemplateResponse(
            request,
            "sessions.html",
            {
                "active": "sessions",
                "database": app.state.database,
                "sessions": _login_session_rows(),
                "message": message,
                "error": error,
            },
        )

    @app.post("/sessions/{competitor_key}/upload")
    async def login_session_upload(request: Request, competitor_key: str, filename: str = ""):
        try:
            competitor = select_competitors([competitor_key], allow_experimental=True)[0]
        except ValueError as exc:
            return RedirectResponse(f"/sessions?error={quote(str(exc))}", status_code=303)
        content = await request.body()
        if not filename.lower().endswith(".json"):
            return RedirectResponse("/sessions?error=Upload%20a%20JSON%20login%20session%20file.", status_code=303)
        try:
            save_uploaded_auth_state(competitor.competitor_key, content)
        except InvalidAuthStateError as exc:
            return RedirectResponse(f"/sessions?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/sessions?message={quote(competitor.display_name + ' login session saved.')}", status_code=303)

    @app.post("/sessions/{competitor_key}/delete")
    def login_session_delete(competitor_key: str):
        try:
            competitor = select_competitors([competitor_key], allow_experimental=True)[0]
        except ValueError as exc:
            return RedirectResponse(f"/sessions?error={quote(str(exc))}", status_code=303)
        delete_auth_state(competitor.competitor_key)
        return RedirectResponse(f"/sessions?message={quote(competitor.display_name + ' login session removed.')}", status_code=303)

    @app.post("/sessions/{competitor_key}/refresh-local")
    def login_session_refresh_local(competitor_key: str):
        try:
            competitor = select_competitors([competitor_key], allow_experimental=True)[0]
            queue_local_login_refresh(competitor.competitor_key)
        except ValueError as exc:
            return RedirectResponse(f"/sessions?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/sessions?message={quote('Desktop Collector will open ' + competitor.display_name + ' login refresh shortly.')}", status_code=303)

    @app.post("/imports/{competitor_key}/refresh-login")
    async def import_refresh_login(request: Request, competitor_key: str):
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        import_batch_id = _int_form_value((parsed.get("import_batch_id") or [""])[-1], 0)
        try:
            competitor = select_competitors([competitor_key], allow_experimental=True)[0]
            queue_local_login_refresh(competitor.competitor_key)
        except ValueError as exc:
            return RedirectResponse(f"/imports?message={quote(str(exc))}", status_code=303)
        query = f"import_batch_id={import_batch_id}&" if import_batch_id else ""
        message = quote(f"{competitor.display_name} login will open on the computer running the Browser Helper.")
        return RedirectResponse(f"/imports?{query}message={message}", status_code=303)

    @app.get("/imports", response_class=HTMLResponse)
    def imports(
        request: Request,
        import_batch_id: int | None = None,
        job_id: str = "",
        view: str = "",
        search: str = "",
        manufacturer: str = "",
        price_position: str = "",
        review_state: str = "",
        competitor_discounted: int = 0,
        missing_competitor_price: int = 0,
        page: int = 1,
        page_size: int = 50,
        message: str = "",
    ):
        job = job_status(job_id) if job_id else current_active_job()
        if job and import_batch_id is None:
            active_import_id = job.get("import_batch_id")
            import_batch_id = int(active_import_id) if active_import_id else None
        terminal_job_statuses = {"completed", "completed_with_warnings", "failed", "stopped_blocked", "stopped_challenge"}
        if view == "results" and job and str(job.get("status") or "") in terminal_job_statuses:
            job = None
        preview = preview_import(app.state.database, import_batch_id) if import_batch_id and view != "results" and not job else None
        page_size = page_size if page_size in PAGE_SIZE_OPTIONS else DEFAULT_PAGE_SIZE
        filters = ComparisonFilters(
            search=search,
            manufacturer=manufacturer,
            price_position=price_position,
            review_state=review_state if review_state in {"pending", "reviewed"} else "",
            competitor_discounted=bool(competitor_discounted),
            missing_competitor_price=bool(missing_competitor_price),
            import_batch_id=import_batch_id,
        )
        comparison_rows_all = comparison_review_rows(app.state.database, filters)
        total = len(comparison_rows_all)
        total_pages = max(1, math.ceil(total / page_size))
        page = min(max(1, page), total_pages)
        offset = (page - 1) * page_size
        comparison_summary = {
            "our_price_higher": sum(1 for row in comparison_rows_all if row.get("price_difference_cents") is not None and row["price_difference_cents"] > 0),
            "our_price_lower": sum(1 for row in comparison_rows_all if row.get("price_difference_cents") is not None and row["price_difference_cents"] < 0),
            "missing_competitor_price": sum(1 for row in comparison_rows_all if not row.get("lowest_competitor_name")),
            "needs_review": sum(1 for row in comparison_rows_all if not row.get("saved_to_catalog")),
        }
        return templates.TemplateResponse(
            request,
            "imports.html",
            {
                "active": "imports",
                "database": app.state.database,
                "history": _import_history_rows(app.state.database),
                "competitors": _competitor_form_options(),
                "login_sessions": _login_session_rows(),
                "local_agent": local_agent_status(),
                "max_upload_mb": 20,
                "preview": preview,
                "job": job,
                "rows": comparison_rows_all[offset : offset + page_size],
                "filters": filters,
                "total": total,
                "summary": comparison_summary,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "message": message,
                "page_query": _comparison_page_query(filters, page_size),
                "quick_filter_queries": _comparison_quick_filter_queries(filters, page_size),
                "selected_import": import_batch_label(app.state.database, import_batch_id),
            },
        )

    @app.post("/imports/upload")
    async def upload_import(request: Request, filename: str = ""):
        content = await request.body()
        try:
            result = save_upload(app.state.database, filename=filename or request.headers.get("x-filename", "upload"), content=content)
        except ValueError as exc:
            response = _imports_response(request, app.state.database, errors=[str(exc)], status_code=400)
            return response
        if all(field in result.auto_mapping.values() for field in REQUIRED_FIELDS):
            return RedirectResponse(f"/imports?import_batch_id={result.import_batch_id}", status_code=303)
        return RedirectResponse(f"/imports/{result.import_batch_id}/map", status_code=303)

    @app.post("/imports/history/clear")
    def imports_clear_history():
        clear_import_history(app.state.database)
        return RedirectResponse("/imports", status_code=303)

    @app.post("/imports/{import_batch_id}/delete")
    def import_delete(import_batch_id: int):
        try:
            delete_import_batch(app.state.database, import_batch_id)
        except ValueError as exc:
            return RedirectResponse(f"/imports?message={quote(str(exc))}", status_code=303)
        return RedirectResponse("/imports?message=Uploaded%20file%20and%20its%20comparison%20rows%20were%20deleted.", status_code=303)

    @app.get("/imports/template")
    def import_template():
        path = IMPORT_DIR / "Part_Pulse_Import_Template.xlsx"
        write_import_template(path)
        return FileResponse(path, filename="Part_Pulse_Import_Template.xlsx")

    @app.get("/imports/{import_batch_id}/map", response_class=HTMLResponse)
    def import_map(request: Request, import_batch_id: int, worksheet: str | None = None):
        from app.imports import _batch, auto_map_headers, default_sheet

        batch = _batch(app.state.database, import_batch_id)
        path = IMPORT_DIR / batch["stored_filename"]
        sheets = workbook_sheets(path)
        selected_sheet = worksheet or batch["worksheet_name"] or default_sheet(sheets)
        headers = read_headers(path, selected_sheet)
        mapping = auto_map_headers(headers)
        return templates.TemplateResponse(
            request,
            "import_map.html",
            {"active": "imports", "database": app.state.database, "batch": batch, "sheets": sheets, "selected_sheet": selected_sheet, "headers": headers, "mapping": mapping, "import_fields": ALL_FIELDS},
        )

    @app.post("/imports/{import_batch_id}/preview", response_class=HTMLResponse)
    async def import_preview(request: Request, import_batch_id: int):
        form = await _urlencoded_form(request)
        worksheet = form.get("worksheet", "")
        mapping = {key.replace("map_", ""): value for key, value in form.items() if key.startswith("map_") and value}
        preview = preview_import(app.state.database, import_batch_id, worksheet=worksheet, mapping=mapping)
        return templates.TemplateResponse(
            request,
            "import_preview.html",
            {"active": "imports", "database": app.state.database, "preview": preview},
        )

    @app.get("/imports/{import_batch_id}/preview", response_class=HTMLResponse)
    def import_preview_get(request: Request, import_batch_id: int):
        preview = preview_import(app.state.database, import_batch_id)
        return templates.TemplateResponse(
            request,
            "import_preview.html",
            {"active": "imports", "database": app.state.database, "preview": preview},
        )

    @app.post("/imports/{import_batch_id}/confirm", response_class=HTMLResponse)
    async def import_confirm(request: Request, import_batch_id: int):
        form = await _urlencoded_form(request)
        worksheet = form.get("worksheet", "")
        mapping = {key.replace("map_", ""): value for key, value in form.items() if key.startswith("map_") and value}
        preview = confirm_import(app.state.database, import_batch_id, worksheet=worksheet, mapping=mapping)
        return templates.TemplateResponse(
            request,
            "import_preview.html",
            {"active": "imports", "database": app.state.database, "preview": preview, "imported": True},
        )

    @app.post("/imports/{import_batch_id}/start-price-check")
    async def import_start_price_check(request: Request, import_batch_id: int):
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] if values else "" for key, values in parsed.items()}
        selected_competitors = parsed.get("competitor", [])
        delay_seconds = _int_form_value(form.get("delay_seconds"), 1)
        preview = confirm_import(app.state.database, import_batch_id)
        if preview.invalid_rows:
            return _imports_response(request, app.state.database, preview=preview, errors=["Fix invalid rows before starting price checks."], status_code=400)
        if not selected_competitors:
            return _imports_response(request, app.state.database, preview=preview, errors=["Select at least one competitor to check."], status_code=400)
        try:
            adapters = select_competitors(selected_competitors)
        except ValueError as exc:
            return _imports_response(request, app.state.database, preview=preview, errors=[str(exc)], status_code=400)
        competitor_keys = [adapter.competitor_key for adapter in adapters]
        parts = plan_import_collection(app.state.database, import_batch_id=import_batch_id)
        errors = validate_collection_request(
            app.state.database,
            parts,
            confirmation="RUN",
            delay_seconds=delay_seconds,
            competitor_keys=competitor_keys,
            max_parts=None,
            require_saved_auth=False,
        )
        if errors:
            return _imports_response(request, app.state.database, preview=preview, errors=errors, status_code=400)
        job_id = queue_local_collection_job(
            app.state.database,
            parts,
            import_batch_id=import_batch_id,
            delay_seconds=delay_seconds,
            competitor_keys=competitor_keys,
        )
        return RedirectResponse(f"/imports?job_id={job_id}", status_code=303)

    @app.get("/imports/{import_batch_id}/validation-errors.csv")
    def import_errors(import_batch_id: int):
        preview = preview_import(app.state.database, import_batch_id)
        return Response(validation_errors_csv(preview), media_type="text/csv")

    @app.get("/collector/imports/{import_batch_id}/input.csv")
    def collector_import_input(import_batch_id: int):
        preview = confirm_import(app.state.database, import_batch_id)
        if preview.invalid_rows:
            return JSONResponse(
                {"status": "invalid_import", "message": "Fix invalid rows before downloading a local collector file."},
                status_code=400,
            )
        content = selected_parts_csv(app.state.database, import_batch_id)
        if not content.strip() or content.count("\n") <= 1:
            return JSONResponse(
                {"status": "empty_import", "message": "No active products were found for this uploaded file."},
                status_code=404,
            )
        return Response(
            content,
            media_type="text/csv",
            headers={"content-disposition": f'attachment; filename="part-pulse-import-{import_batch_id}-collector-input.csv"'},
        )

    @app.post("/collector/results/upload")
    async def collector_results_upload(request: Request, competitor: str = "", filename: str = "", job_id: str = ""):
        content = await request.body()
        upload_dir = OUTPUT_DIR / "collector_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(char for char in (filename or request.headers.get("x-filename") or "collection_summary.csv") if char.isalnum() or char in ("-", "_", "."))
        saved_path = upload_dir / f"{Path(safe_name).stem}-{quote(competitor or 'unknown', safe='')}.csv"
        saved_path.write_bytes(content)
        result = import_collection_summary(app.state.database, summary_csv=content, fallback_competitor=competitor or None)
        if job_id:
            current = job_status(job_id)
            if current.get("status") != "not_found":
                current["message"] = f"Imported {result.rows_imported} {result.competitor} results."
        return JSONResponse(
            {
                "status": "imported",
                "scan_run_id": result.scan_run_id,
                "competitor": result.competitor,
                "rows_received": result.rows_received,
                "rows_imported": result.rows_imported,
                "rows_skipped": result.rows_skipped,
                "successful_rows": result.successful_rows,
            }
        )

    @app.post("/collector/agent/jobs/next")
    def collector_agent_next(agent_id: str):
        job = claim_next_local_job(agent_id)
        if job is None:
            return Response(status_code=204)
        job_id = str(job["job_id"])
        return JSONResponse(
            {
                "job_id": job_id,
                "import_batch_id": job.get("import_batch_id"),
                "competitors": job.get("competitors", []),
                "planned_count": job.get("planned_count", 0),
                "collection_mode": job.get("collection_mode", "full_browser"),
                "delay_seconds": int(job.get("delay_seconds") or 1),
                "input_url": f"/collector/agent/jobs/{job_id}/input.csv",
            }
        )

    @app.post("/collector/agent/login/next")
    def collector_agent_login_next(agent_id: str):
        request = claim_next_local_login_refresh(agent_id)
        if request is None:
            return Response(status_code=204)
        return JSONResponse(
            {
                "request_id": request.get("request_id"),
                "competitor_key": request.get("competitor_key"),
                "display_name": request.get("display_name"),
            }
        )

    @app.post("/collector/agent/heartbeat")
    def collector_agent_heartbeat(agent_id: str):
        return JSONResponse(register_local_agent(agent_id))

    @app.get("/collector/agent/status")
    def collector_agent_status():
        return JSONResponse(local_agent_status())

    @app.get("/collector/agent/jobs/{job_id}/input.csv")
    def collector_agent_job_input(job_id: str):
        path = local_job_input_path(job_id)
        if path is None:
            return JSONResponse({"status": "not_found", "message": "Collector input was not found."}, status_code=404)
        return FileResponse(path, media_type="text/csv", filename=f"part-pulse-job-{job_id}.csv")

    @app.post("/collector/agent/jobs/{job_id}/progress/{competitor_key}")
    async def collector_agent_job_progress(request: Request, job_id: str, competitor_key: str, agent_id: str):
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"status": "invalid", "message": "Progress must be a JSON object."}, status_code=400)
        try:
            update_local_job_progress(job_id, competitor_key, payload, agent_id)
        except (FileNotFoundError, ValueError) as exc:
            return JSONResponse({"status": "invalid", "message": str(exc)}, status_code=404)
        return JSONResponse({"status": "updated"})

    @app.post("/collector/agent/jobs/{job_id}/complete")
    async def collector_agent_job_complete(request: Request, job_id: str, agent_id: str):
        payload = await request.json()
        try:
            metadata = complete_local_job(
                job_id,
                status=str(payload.get("status") or "failed"),
                message=str(payload.get("message") or "Local collection finished."),
                agent_id=agent_id,
            )
        except FileNotFoundError as exc:
            return JSONResponse({"status": "not_found", "message": str(exc)}, status_code=404)
        return JSONResponse({"status": metadata["status"]})

    @app.get("/collections/test", response_class=HTMLResponse)
    def collection_test(request: Request, manufacturer: str = "", limit: int = 25):
        parts = plan_ui_collection(app.state.database, manufacturer=manufacturer, limit=limit)
        return templates.TemplateResponse(
            request,
            "collection_test.html",
            {"active": "collections", "database": app.state.database, "parts": parts, "manufacturer": manufacturer, "limit": limit},
        )

    @app.post("/collections/test", response_class=HTMLResponse)
    async def collection_start(request: Request):
        form = await _urlencoded_form(request)
        confirmation = form.get("confirmation", "")
        manufacturer = form.get("manufacturer", "")
        limit = _int_form_value(form.get("limit"), 25)
        delay_seconds = _int_form_value(form.get("delay_seconds"), 1)
        parts = plan_ui_collection(app.state.database, manufacturer=manufacturer, limit=limit)
        errors = validate_collection_request(app.state.database, parts, confirmation=confirmation, delay_seconds=delay_seconds)
        if errors:
            return templates.TemplateResponse(
                request,
                "collection_test.html",
                {"active": "collections", "database": app.state.database, "parts": parts, "manufacturer": manufacturer, "limit": limit, "errors": errors},
                status_code=400,
            )
        job_id = start_price_collection_job(app.state.database, parts, delay_seconds=delay_seconds)
        return RedirectResponse(f"/collections/jobs/{job_id}", status_code=303)

    @app.get("/collections/jobs/{job_id}", response_class=HTMLResponse)
    def collection_job(request: Request, job_id: str):
        return templates.TemplateResponse(
            request,
            "collection_job.html",
            {"active": "collections", "database": app.state.database, "job": job_status(job_id)},
        )

    @app.get("/collections/jobs/{job_id}/status")
    def collection_job_status(job_id: str):
        return JSONResponse(job_status(job_id))

    @app.get("/runs/{scan_run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, scan_run_id: int):
        data = scan_run_detail(app.state.database, scan_run_id)
        if data is None:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": "Scan run not found.", "database": app.state.database, "active": "runs"},
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {"active": "runs", "database": app.state.database, **data},
        )

    @app.get("/quality", response_class=HTMLResponse)
    def quality(request: Request):
        return templates.TemplateResponse(
            request,
            "quality.html",
            {"active": "quality", "database": app.state.database, **quality_data(app.state.database)},
        )

    return app


async def _urlencoded_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _safe_back_url(value: str) -> str:
    """Only allow same-site paths, so a crafted link cannot send the user
    somewhere else."""
    candidate = (value or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return ""
    return candidate


def _format_price(value: object) -> str:
    if value is None or value == "":
        return ""
    cleaned = str(value).strip().replace("$", "").replace(",", "")
    try:
        return f"{Decimal(cleaned):,.2f}"
    except InvalidOperation:
        return str(value).replace("$", "")


def _import_history_rows(database: Path) -> list[dict[str, object]]:
    rows = import_history(database)
    completed_statuses = {"completed", "completed_with_warnings"}
    terminal_statuses = completed_statuses | {"failed", "stopped_blocked", "stopped_challenge"}
    for row in rows:
        job = latest_job_for_import(int(row["import_batch_id"]))
        scan_status = str(job.get("status") or "") if job else ""
        row["scan_status"] = scan_status
        row["scan_completed"] = scan_status in completed_statuses
        row["scan_terminal"] = scan_status in terminal_statuses
        row["scan_job_id"] = str(job.get("job_id") or "") if job else ""
        row["scan_message"] = str(job.get("message") or "") if job else ""
        row["scan_detail"] = str(job.get("failure_reason") or "") if job else ""
        progress = job.get("progress") if job else {}
        row["scan_has_results"] = bool(isinstance(progress, dict) and int(progress.get("completed") or 0) > 0)
    return rows


def _int_form_value(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def _optional_int_form_value(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _competitor_form_options():
    options = []
    for competitor in list_competitors():
        auth_saved = auth_state_exists(competitor.competitor_key)
        can_run_now = competitor.capabilities.status == "active"
        options.append(
            {
                "competitor_key": competitor.competitor_key,
                "display_name": competitor.display_name,
                "capabilities": competitor.capabilities,
                "requires_login": competitor.requires_login,
                "auth_state_saved": auth_saved,
                "can_run_now": can_run_now,
                "default_checked": can_run_now,
            }
        )
    return options


def _login_session_rows() -> list[dict[str, object]]:
    rows = []
    for competitor in list_competitors():
        status = auth_state_status(competitor.competitor_key)
        rows.append(
            {
                "competitor_key": competitor.competitor_key,
                "display_name": competitor.display_name,
                "requires_login": competitor.requires_login,
                "auth_state_saved": bool(status["exists"]),
                "updated_at": _timestamp_from_epoch(status["updated_at"]),
                "ready": not competitor.requires_login or bool(status["exists"]),
            }
        )
    return rows


def _timestamp_from_epoch(value: object) -> str:
    if value is None:
        return ""
    try:
        from datetime import datetime

        return datetime.fromtimestamp(float(value), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return ""


def _comparison_page_query(filters: ComparisonFilters, page_size: int) -> str:
    params: dict[str, str | int] = {"page_size": page_size}
    if filters.search:
        params["search"] = filters.search
    if filters.manufacturer:
        params["manufacturer"] = filters.manufacturer
    if filters.price_position:
        params["price_position"] = filters.price_position
    if filters.competitor_discounted:
        params["competitor_discounted"] = 1
    if filters.scan_priority:
        params["scan_priority"] = filters.scan_priority
    if filters.missing_competitor_price:
        params["missing_competitor_price"] = 1
    if filters.hidden_competitor_price:
        params["hidden_competitor_price"] = 1
    if filters.needs_review:
        params["needs_review"] = 1
    if filters.review_state:
        params["review_state"] = filters.review_state
    if filters.import_batch_id:
        params["import_batch_id"] = filters.import_batch_id
    return urlencode(params)


def _comparison_quick_filter_queries(filters: ComparisonFilters, page_size: int) -> dict[str, str]:
    base: dict[str, str | int] = {"page_size": page_size}
    if filters.import_batch_id:
        base["import_batch_id"] = filters.import_batch_id
    return {
        "all": urlencode(base),
        "above": urlencode(base | {"price_position": "above"}),
        "below": urlencode(base | {"price_position": "below"}),
        "missing": urlencode(base | {"missing_competitor_price": 1}),
    }


def _imports_response(
    request: Request,
    database: Path,
    *,
    preview=None,
    job=None,
    errors: list[str] | None = None,
    status_code: int = 200,
):
    page_size = 50
    import_batch_id = preview.import_batch_id if preview else None
    filters = ComparisonFilters(import_batch_id=import_batch_id)
    comparison_rows_all = comparison_review_rows(database, filters)
    total = len(comparison_rows_all)
    total_pages = max(1, math.ceil(total / page_size))
    comparison_summary = {
        "our_price_higher": sum(1 for row in comparison_rows_all if row.get("price_difference_cents") is not None and row["price_difference_cents"] > 0),
        "our_price_lower": sum(1 for row in comparison_rows_all if row.get("price_difference_cents") is not None and row["price_difference_cents"] < 0),
        "missing_competitor_price": sum(1 for row in comparison_rows_all if not row.get("lowest_competitor_name")),
        "needs_review": sum(1 for row in comparison_rows_all if not row.get("saved_to_catalog")),
    }
    return templates.TemplateResponse(
        request,
        "imports.html",
        {
            "active": "imports",
            "database": database,
            "history": _import_history_rows(database),
            "competitors": _competitor_form_options(),
            "login_sessions": _login_session_rows(),
            "local_agent": local_agent_status(),
            "max_upload_mb": 20,
            "preview": preview,
            "job": job,
            "errors": errors or [],
            "rows": comparison_rows_all[:page_size],
            "filters": filters,
            "total": total,
            "summary": comparison_summary,
            "page": 1,
            "page_size": page_size,
            "total_pages": total_pages,
            "message": "",
            "page_query": _comparison_page_query(filters, page_size),
            "quick_filter_queries": _comparison_quick_filter_queries(filters, page_size),
            "selected_import": import_batch_label(database, import_batch_id),
        },
        status_code=status_code,
    )
