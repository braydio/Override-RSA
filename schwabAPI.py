"""Schwab brokerage automation module.

This module provides helpers for logging in to Schwab accounts, retrieving
holdings, and placing trades using the :mod:`schwab_api` package.  Additional
debug logging is included to troubleshoot login and trading issues.
"""

# Nelson Dane
# Schwab API

import os
import traceback
from time import sleep

from dotenv import load_dotenv
from schwab_api import Schwab, urls

from helperAPI import Brokerage, maskString, printAndDiscord, printHoldings, stockOrder


def _ensure_cache_dir(preferred: str, fallback: str):
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except Exception as e:
        print(f"Warning: Could not create cache dir '{preferred}': {e}")
        try:
            os.makedirs(fallback, exist_ok=True)
            print(f"Using fallback cache dir: {fallback}")
            return fallback
        except Exception as e2:
            print(f"Warning: Could not create fallback cache dir '{fallback}': {e2}")
            return preferred


def _diagnose_positions(schwab: Schwab):
    try:
        sess = schwab.get_session()
        r = sess.get(urls.positions_v2(), headers=schwab.headers)
        print(f"positions_v2 status_code={r.status_code}")
        snippet = r.text[:500].replace("\n", " ")
        print(f"positions_v2 response (first 500 chars): {snippet}")
        return r.status_code
    except Exception as e:
        print(f"Warning: Unable to fetch positions_v2 for diagnostics: {e}")
        return None


def _get_account_info_guarded(schwab: Schwab, name: str):
    try:
        account_info = schwab.get_account_info_v2()
        if not isinstance(account_info, dict):
            raise TypeError("Account info is not a dict")
        return account_info
    except Exception as info_error:
        print(f"Error retrieving account info for {name}: {info_error}")
        status = _diagnose_positions(schwab)
        return None, status


def schwab_init(SCHWAB_EXTERNAL=None):
    """Log into Schwab and return a :class:`Brokerage` object.

    Parameters
    ----------
    SCHWAB_EXTERNAL : str, optional
        Comma separated credentials in the form
        ``username:password:totp``. When ``None``, credentials are read from
        the ``SCHWAB`` environment variable.

    The function prints detailed information about each login attempt and, on
    failure, logs the HTTP response from the Schwab holdings endpoint to aid
    debugging.
    """

    # Initialize .env file and gather credentials
    load_dotenv()
    schwab_env = os.getenv("SCHWAB")
    if not schwab_env and SCHWAB_EXTERNAL is None:
        print("Schwab not found, skipping...")
        return None

    accounts = (
        schwab_env.strip().split(",")
        if SCHWAB_EXTERNAL is None
        else SCHWAB_EXTERNAL.strip().split(",")
    )

    print(f"Accounts provided: {accounts}")
    print("Logging in to Schwab...")

    schwab_obj = Brokerage("Schwab")
    for account in accounts:
        index = accounts.index(account) + 1
        name = f"Schwab {index}"
        try:
            account = account.split(":")
            username = account[0]
            cache_dir = _ensure_cache_dir("creds", ".creds")
            print(
                f"Starting login {index} for {maskString(username)} with cache {os.path.join(cache_dir, f'schwab{index}.json')}"
            )
            session_cache_path = os.path.join(cache_dir, f"schwab{index}.json")
            schwab = Schwab(session_cache=session_cache_path)
            totp = None if account[2] == "NA" else account[2]
            if totp:
                print("Using provided TOTP secret")
            else:
                print("No TOTP secret provided")

            success = schwab.login(
                username=username,
                password=account[1],
                totp_secret=totp,
            )
            print(f"Login result for {name}: {success}")

            # Fetch account info with diagnostics and retry logic
            result = _get_account_info_guarded(schwab, name)
            if isinstance(result, tuple):
                account_info, status = result
            else:
                account_info, status = result, None

            if account_info is None:
                # If we got a 431 or non-JSON response, try a cache reset and re-login
                should_retry = status == 431 or status is None
                if should_retry:
                    print(
                        f"{name}: Retrying login with fresh session cache due to invalid account info (status={status})"
                    )
                    try:
                        try:
                            os.remove(session_cache_path)
                            print(f"Removed stale session cache: {session_cache_path}")
                        except Exception:
                            pass
                        fresh_cache_path = os.path.join(
                            cache_dir, f"schwab{index}_fresh.json"
                        )
                        schwab = Schwab(session_cache=fresh_cache_path)
                        success = schwab.login(
                            username=username,
                            password=account[1],
                            totp_secret=None if account[2] == "NA" else account[2],
                        )
                        print(f"Login result for {name} (fresh session): {success}")
                        result2 = _get_account_info_guarded(schwab, name)
                        if isinstance(result2, tuple):
                            account_info, status2 = result2
                        else:
                            account_info, status2 = result2, None
                        if account_info is None:
                            print(
                                f"{name}: Still unable to retrieve account info (status={status2}). Aborting."
                            )
                            raise Exception(
                                "Unable to retrieve Schwab account info after retry"
                            )
                    except Exception:
                        raise

            account_list = list(account_info.keys())
            print_accounts = [maskString(a) for a in account_list]
            print(f"The following Schwab accounts were found: {print_accounts}")
            print("Logged in to Schwab!")
            schwab_obj.set_logged_in_object(name, schwab)
            for account in account_list:
                schwab_obj.set_account_number(name, account)
                schwab_obj.set_account_totals(
                    name, account, account_info[account]["account_value"]
                )
        except Exception as e:
            print(f"Error logging in to Schwab: {e}")
            print(traceback.format_exc())
            return None
    return schwab_obj


def schwab_holdings(schwab_o: Brokerage, loop=None):
    """Retrieve holdings for all logged in Schwab accounts."""

    for key in schwab_o.get_account_numbers():
        print(f"Gathering holdings for {key}")
        obj: Schwab = schwab_o.get_logged_in_objects(key)
        try:
            all_holdings = obj.get_account_info_v2()
            if not isinstance(all_holdings, dict):
                raise TypeError("Account info is not a dict")
        except Exception as e:
            printAndDiscord(f"{key}: Error getting account info: {e}", loop)
            _diagnose_positions(obj)
            continue
        for account in schwab_o.get_account_numbers(key):
            print(f"Processing account {maskString(account)}")
            try:
                holdings = all_holdings[account]["positions"]
                for item in holdings:
                    sym = item["symbol"] or "Unknown"
                    mv = round(float(item["market_value"]), 2)
                    qty = float(item["quantity"])
                    # Schwab doesn't return current price, so we have to calculate it
                    current_price = 0 if qty == 0 else round(mv / qty, 2)
                    schwab_o.set_holdings(key, account, sym, qty, current_price)
            except Exception as e:
                printAndDiscord(f"{key} {account}: Error getting holdings: {e}", loop)
                print(traceback.format_exc())
    printHoldings(schwab_o, loop)


def schwab_transaction(schwab_o: Brokerage, orderObj: stockOrder, loop=None):
    """Execute trades on each Schwab account using ``orderObj``."""

    print()
    print("==============================")
    print("Schwab")
    print("==============================")
    print()
    # Use each account (unless specified in .env)
    purchase_accounts = os.getenv("SCHWAB_ACCOUNT_NUMBERS", "").strip().split(":")
    print(
        f"Restricted accounts: {purchase_accounts if purchase_accounts != [''] else 'None'}"
    )
    for s in orderObj.get_stocks():
        print(f"Submitting orders for {s}")
        for key in schwab_o.get_account_numbers():
            printAndDiscord(
                f"{key} {orderObj.get_action()}ing {orderObj.get_amount()} {s} @ {orderObj.get_price()}",
                loop,
            )
            obj: Schwab = schwab_o.get_logged_in_objects(key)
            for account in schwab_o.get_account_numbers(key):
                print_account = maskString(account)
                print(f"Handling account {print_account}")
                if (
                    purchase_accounts != [""]
                    and orderObj.get_action().lower() != "sell"
                    and str(account) not in purchase_accounts
                ):
                    print(
                        f"Skipping account {print_account}, not in SCHWAB_ACCOUNT_NUMBERS"
                    )
                    continue
                # If DRY is True, don't actually make the transaction
                if orderObj.get_dry():
                    printAndDiscord(
                        "Running in DRY mode. No transactions will be made.", loop
                    )
                try:
                    messages, success = obj.trade_v2(
                        ticker=s,
                        side=orderObj.get_action().capitalize(),
                        qty=orderObj.get_amount(),
                        account_id=account,
                        dry_run=orderObj.get_dry(),
                    )
                    print(f"trade_v2 returned success={success} messages={messages}")
                    printAndDiscord(
                        (
                            f"{key} account {print_account}: The order verification was "
                            + "successful"
                            if success
                            else "unsuccessful, retrying..."
                        ),
                        loop,
                    )
                    if not success:
                        messages, success = obj.trade(
                            ticker=s,
                            side=orderObj.get_action().capitalize(),
                            qty=orderObj.get_amount(),
                            account_id=account,
                            dry_run=orderObj.get_dry(),
                        )
                        print(
                            f"trade retry returned success={success} messages={messages}"
                        )
                        printAndDiscord(
                            (
                                f"{key} account {print_account}: The order verification was "
                                + "retry successful"
                                if success
                                else "retry unsuccessful"
                            ),
                            loop,
                        )
                        printAndDiscord(
                            f"{key} account {print_account}: The order verification produced the following messages: {messages}",
                            loop,
                        )
                except Exception as e:
                    printAndDiscord(
                        f"{key} {print_account}: Error submitting order: {e}", loop
                    )
                    print(traceback.format_exc())
                sleep(1)
