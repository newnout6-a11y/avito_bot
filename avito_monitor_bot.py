"""Public entry point for the Avito monitor bot.

The Telegram handlers live in :mod:`avito_ui`; this module keeps the historic
imports and executable entry point stable for deployments and integrations.
"""

from avito_ui import (  # noqa: F401
    API_URLS_FILE,
    KEY_RE,
    MAIN_INLINE_KB,
    Ad,
    App,
    FeedItem,
    LicenseManager,
    SearchWizard,
    SubscriberFilter,
    Subscription,
    Watcher,
    WatcherManager,
    _attr_to_str,
    _br,
    _extract_ad_id,
    _fmt_dt,
    _get_text,
    _parse_price_input,
    account_panel_text,
    avito_short_url,
    build_account_kb,
    build_searches_kb,
    build_sub_inline_kb,
    format_sub_panel,
    get_watcher_status,
    is_valid_avito_url,
    main,
    parse_api_items,
    search_key_from_url,
    try_extract_filters_from_url,
    wizard_got_url,
)


def run() -> None:
    import asyncio

    asyncio.run(main())

__all__ = [name for name in globals() if not name.startswith("__")]


if __name__ == "__main__":

    try:
        run()
    except (KeyboardInterrupt, SystemExit):
        pass
