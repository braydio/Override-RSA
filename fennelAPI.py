import os
import pickle
import requests
import logging
import asyncio
import traceback
from dotenv import load_dotenv
from fennel_invest_api.endpoints import Endpoints
from helperAPI import Brokerage, getOTPCodeDiscord, printAndDiscord, printHoldings, stockOrder

# Configure logging (if not already configured elsewhere)
logging.basicConfig(level=logging.DEBUG)


def check_login(func):
    def wrapper(self, *args, **kwargs):
        if self.Bearer is None:
            raise Exception("Bearer token is not set. Please login first.")
        return func(self, *args, **kwargs)
    return wrapper


class Fennel:
    def __init__(self, filename="fennel_credentials.pkl", path=None) -> None:
        self.session = requests.Session()
        self.endpoints = Endpoints()
        self.Bearer = None
        self.Refresh = None
        self.ID_Token = None
        self.timeout = 10
        self.account_ids = []  # For multiple accounts
        self.client_id = "FXGlhcVdamwozAFp8BZ2MWl6coPl6agX"
        self.filename = filename
        self.path = path  # Already given as a parameter
        if self.path is not None and not os.path.exists(self.path):
            os.makedirs(self.path)
        self._load_credentials()

    def _load_credentials(self):
        filename = self.filename if self.path is None else os.path.join(self.path, self.filename)
        if os.path.exists(filename):
            with open(filename, "rb") as f:
                credentials = pickle.load(f)
            self.Bearer = credentials.get("Bearer")
            self.Refresh = credentials.get("Refresh")
            self.ID_Token = credentials.get("ID_Token")
            self.client_id = credentials.get("client_id", self.client_id)
            logging.debug(f"Loaded credentials from {filename}")
        else:
            logging.debug(f"No credentials file found at {filename}")

    def _save_credentials(self):
        filename = self.filename if self.path is None else os.path.join(self.path, self.filename)
        with open(filename, "wb") as f:
            pickle.dump(
                {
                    "Bearer": self.Bearer,
                    "Refresh": self.Refresh,
                    "ID_Token": self.ID_Token,
                    "client_id": self.client_id,
                },
                f,
            )
        logging.debug(f"Saved credentials to {filename}")

    def _clear_credentials(self):
        filename = self.filename if self.path is None else os.path.join(self.path, self.filename)
        if os.path.exists(filename):
            os.remove(filename)
            logging.debug(f"Removed credentials file {filename}")
        self.Bearer = None
        self.Refresh = None
        self.ID_Token = None

    def login(self, email, wait_for_code=True, code=None):
        # If creds exist, check if they are valid/try to refresh
        if self.Bearer is not None and self._verify_login():
            return True

        if code is None:
            url = self.endpoints.retrieve_bearer_url()
            payload = {
                "email": email,
                "client_id": self.client_id,
                "connection": "email",
                "send": "code",
            }
            logging.debug(f"Starting passwordless login request to {url} with payload: {payload}")
            response = self.session.post(url, json=payload, timeout=self.timeout)
            logging.debug(f"Response: {response.status_code} {response.text}")
            if response.status_code != 200:
                raise Exception(f"Failed to start passwordless login: {response.text}")
            if not wait_for_code:
                raise Exception("2FA required. Please provide the code.")
            print("2FA code sent to email")
            code = input("Enter 2FA code: ")

        url = self.endpoints.oauth_url()
        payload = {
            "grant_type": "http://auth0.com/oauth/grant-type/passwordless/otp",
            "client_id": self.client_id,
            "otp": str(code),
            "username": email,
            "scope": "openid profile offline_access email",
            "audience": "https://meta.api.fennel.com/graphql",
            "realm": "email",
        }
        logging.debug(f"Starting OAuth login request to {url} with payload: {payload}")
        response = self.session.post(url, json=payload, timeout=self.timeout)
        logging.debug(f"Response: {response.status_code} {response.text}")
        if response.status_code != 200:
            raise Exception(f"Failed to login: {response.text}")
        response_json = response.json()
        self.Bearer = response_json["access_token"]
        self.Refresh = response_json["refresh_token"]
        self.ID_Token = response_json["id_token"]
        self.refresh_token()
        self.get_account_ids()
        return True

    def refresh_token(self):
        url = self.endpoints.oauth_url()
        headers = self.endpoints.build_headers(accounts_host=True)
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": self.Refresh,
            "scope": "openid profile offline_access email",
        }
        logging.debug(f"Refreshing token with POST to {url} and payload: {payload}")
        response = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        logging.debug(f"Response: {response.status_code} {response.text}")
        if response.status_code != 200:
            raise Exception(f"Failed to refresh bearer token: {response.text}")
        response_json = response.json()
        self.Bearer = response_json["access_token"]
        self.Refresh = response_json["refresh_token"]
        self.ID_Token = response_json["id_token"]
        self._save_credentials()
        return response_json

    def _verify_login(self):
        # Test login by getting Account IDs
        try:
            self.get_account_ids()
            return True
        except Exception:
            try:
                self.refresh_token()
                self.get_account_ids()
                return True
            except Exception as e:
                logging.error(f"Failed to refresh token: {e}")
                self._clear_credentials()
                return False

    @check_login
    def get_account_ids(self):
        query = self.endpoints.account_ids_query()
        headers = self.endpoints.build_headers(self.Bearer)
        logging.debug(f"Requesting account IDs with GraphQL query: {query}")
        response = self.session.post(
            self.endpoints.graphql, headers=headers, data=query, timeout=self.timeout
        )
        logging.debug(f"Response from get_account_ids: {response.status_code} {response.text}")
        if response.status_code != 200:
            raise Exception(
                f"Account ID Check failed with status code {response.status_code}: {response.text}"
            )
        response_json = response.json()
        logging.debug(f"JSON response from get_account_ids: {response_json}")
        data = response_json.get("data", {})
        user = data.get("user", {})
        accounts = user.get("accounts")
        if accounts is None:
            raise Exception("Accounts data not found in response.")
        response_list = sorted(accounts, key=lambda x: x["created"])
        account_ids = []
        for account in response_list:
            if account["status"] == "APPROVED":
                account_ids.append(account["id"])
        self.account_ids = account_ids
        return account_ids

    @check_login
    def get_portfolio_summary(self, account_id):
        query = self.endpoints.portfolio_query(account_id)
        headers = self.endpoints.build_headers(self.Bearer)
        logging.debug(f"Requesting portfolio summary for account {account_id} with query: {query}")
        response = self.session.post(
            self.endpoints.graphql, headers=headers, data=query, timeout=self.timeout
        )
        logging.debug(f"Response from get_portfolio_summary: {response.status_code} {response.text}")
        if response.status_code != 200:
            raise Exception(
                f"Portfolio Request failed with status code {response.status_code}: {response.text}"
            )
        response_json = response.json()
        return response_json["data"]["account"]["portfolio"]

    @check_login
    def get_stock_quote(self, ticker):
        query = self.endpoints.stock_search_query(ticker, 20)
        headers = self.endpoints.build_headers(self.Bearer)
        logging.debug(f"Requesting stock quote for ticker {ticker} with query: {query}")
        search_response = self.session.post(
            self.endpoints.graphql, headers=headers, data=query, timeout=self.timeout
        )
        logging.debug(f"Response from get_stock_quote: {search_response.status_code} {search_response.text}")
        if search_response.status_code != 200:
            raise Exception(
                f"Stock Search Request failed with status code {search_response.status_code}: {search_response.text}"
            )
        search_json = search_response.json()
        securities = search_json["data"]["searchSearch"]["searchSecurities"]
        if len(securities) == 0:
            raise Exception(
                f"No stock found with ticker {ticker}. Please check the app to see if it is valid."
            )
        stock_quote = next(
            (
                x
                for x in securities
                if x["security"]["ticker"].lower() == ticker.lower()
            ),
            None,
        )
        return stock_quote

    @check_login
    def get_stock_price(self, ticker):
        quote = self.get_stock_quote(ticker)
        return None if quote is None else quote["security"]["currentStockPrice"]

    @check_login
    def get_stock_holdings(self, account_id):
        query = self.endpoints.stock_holdings_query(account_id)
        headers = self.endpoints.build_headers(self.Bearer)
        logging.debug(f"Requesting stock holdings for account {account_id} with query: {query}")
        response = self.session.post(
            self.endpoints.graphql, headers=headers, data=query, timeout=self.timeout
        )
        logging.debug(f"Response from get_stock_holdings: {response.status_code} {response.text}")
        if response.status_code != 200:
            raise Exception(
                f"Stock Holdings Request failed with status code {response.status_code}: {response.text}"
            )
        response_json = response.json()
        return response_json["data"]["account"]["portfolio"]["bulbs"]

    @check_login
    def is_market_open(self):
        query = self.endpoints.is_market_open_query()
        headers = self.endpoints.build_headers(self.Bearer)
        logging.debug(f"Requesting market open status with query: {query}")
        response = self.session.post(
            self.endpoints.graphql, headers=headers, data=query, timeout=self.timeout
        )
        logging.debug(f"Response from is_market_open: {response.status_code} {response.text}")
        if response.status_code != 200:
            raise Exception(
                f"Market Open Request failed with status code {response.status_code}: {response.text}"
            )
        response_json = response.json()
        return response_json["data"]["securityMarketInfo"]["isOpen"]

    @check_login
    def get_stock_isin(self, ticker):
        quote = self.get_stock_quote(ticker)
        return None if quote is None else quote["isin"]

    @check_login
    def is_stock_tradable(self, isin, account_id, side="buy"):
        query = self.endpoints.is_tradable_query(isin, account_id)
        headers = self.endpoints.build_headers(self.Bearer)
        logging.debug(f"Requesting tradable status for ISIN {isin} on account {account_id} with query: {query}")
        response = self.session.post(
            self.endpoints.graphql, headers=headers, data=query, timeout=self.timeout
        )
        logging.debug(f"Response from is_stock_tradable: {response.status_code} {response.text}")
        if response.status_code != 200:
            raise Exception(
                f"Tradable Request failed with status code {response.status_code}: {response.text}"
            )
        response_json = response.json()
        can_trade = response_json["data"]["bulbBulb"]["tradeable"]
        if can_trade is None:
            return False, "No tradeable data found"
        if side.lower() == "buy":
            return can_trade["canBuy"], can_trade["restrictionReason"]
        return can_trade["canSell"], can_trade["restrictionReason"]

    @check_login
    def get_stock_info_from_holdings(self, account_id, ticker) -> dict | None:
        holdings = self.get_stock_holdings(account_id)
        stock_info = next(
            (x for x in holdings if x["security"]["ticker"].lower() == ticker.lower()),
            None,
        )
        return stock_info

    @check_login
    def place_order(self, account_id, ticker, quantity, side, price="market", dry_run=False):
        if side.lower() not in ["buy", "sell"]:
            raise Exception("Side must be either 'buy' or 'sell'")
        # Check if market is open
        if not self.is_market_open():
            raise Exception("Market is closed. Cannot place order.")
        # Search for stock "isin"
        isin = self.get_stock_isin(ticker)
        if isin is None and side.lower() == "sell":
            # Can't get from app search, try holdings
            stock_info = self.get_stock_info_from_holdings(account_id, ticker)
            if stock_info is not None:
                isin = stock_info["isin"]
        if isin is None:
            raise Exception(f"Failed to find ISIN for stock with ticker {ticker}")
        # Check if stock is tradable
        can_trade, restriction_reason = self.is_stock_tradable(isin, account_id, side)
        if not can_trade:
            raise Exception(f"Stock {ticker} is not tradable: {restriction_reason}")
        if dry_run:
            return {
                "account_id": account_id,
                "ticker": ticker,
                "quantity": quantity,
                "isin": isin,
                "side": side,
                "price": price,
                "dry_run_success": True,
            }
        # Place order
        query = self.endpoints.stock_order_query(account_id, ticker, quantity, isin, side, price)
        headers = self.endpoints.build_headers(self.Bearer)
        logging.debug(f"Placing order with query: {query}")
        order_response = self.session.post(
            self.endpoints.graphql, headers=headers, data=query, timeout=self.timeout
        )
        logging.debug(f"Response from place_order: {order_response.status_code} {order_response.text}")
        if order_response.status_code != 200:
            raise Exception(
                f"Order Request failed with status code {order_response.status_code}: {order_response.text}"
            )
        return order_response.json()


def get_otp_and_login(fb, account, name, botObj, loop):
    """
    Retrieve the OTP code from Discord and attempt login.
    """
    timeout = 300  # seconds
    logging.debug(f"{name}: Waiting for OTP code from Discord (timeout: {timeout}s)...")
    otp_code = asyncio.run_coroutine_threadsafe(
        getOTPCodeDiscord(botObj, name, timeout=timeout, loop=loop),
        loop,
    ).result()
    logging.debug(f"{name}: Received OTP code: {otp_code}")
    if otp_code is None:
        raise Exception("No 2FA code found")
    logging.debug(f"{name}: Attempting login with OTP code: {otp_code}")
    try:
        fb.login(email=account, wait_for_code=False, code=otp_code)
    except Exception as e:
        logging.error(f"{name}: Exception during OTP login: {e}")
        raise


def fennel_init(FENNEL_EXTERNAL=None, botObj=None, loop=None):
    load_dotenv()
    fennel_obj = Brokerage("Fennel")
    if not os.getenv("FENNEL") and FENNEL_EXTERNAL is None:
        logging.info("Fennel not found in .env, skipping initialization...")
        return None
    FENNEL = (
        os.environ["FENNEL"].strip().split(",")
        if FENNEL_EXTERNAL is None
        else FENNEL_EXTERNAL.strip().split(",")
    )
    logging.info("Starting login process for Fennel accounts...")
    for index, account in enumerate(FENNEL):
        name = f"Fennel {index + 1}"
        try:
            logging.info(f"{name}: Attempting login for email: {account}")
            fb = Fennel(filename=f"fennel{index + 1}.pkl", path="./creds/")
            try:
                if botObj is None and loop is None:
                    logging.debug(f"{name}: Logging in from CLI (waiting for OTP if required)...")
                    fb.login(email=account, wait_for_code=True)
                else:
                    logging.debug(f"{name}: Logging in from Discord (not waiting for OTP initially)...")
                    fb.login(email=account, wait_for_code=False)
            except Exception as e:
                if "2FA" in str(e) and botObj is not None and loop is not None:
                    logging.info(f"{name}: 2FA required, retrieving OTP via Discord...")
                    get_otp_and_login(fb, account, name, botObj, loop)
                else:
                    try:
                        raw_resp = fb.last_response.text if hasattr(fb, 'last_response') else 'No last_response available'
                        logging.error(f"{name}: Raw API response during login: {raw_resp}")
                    except Exception:
                        logging.error(f"{name}: Could not retrieve raw API response.")
                    logging.error(f"{name}: Error during initial login attempt: {e}")
                    logging.error(traceback.format_exc())
                    raise e

            logging.debug(f"{name}: Login accepted, retrieving account IDs...")
            try:
                account_ids = fb.get_account_ids()
            except Exception as e:
                try:
                    raw_resp = fb.last_response.text if hasattr(fb, 'last_response') else 'No last_response available'
                    logging.error(f"{name}: Raw API response when retrieving account IDs: {raw_resp}")
                except Exception:
                    logging.error(f"{name}: Could not retrieve raw API response for account IDs.")
                logging.error(f"{name}: Exception during get_account_ids: {e}")
                raise e

            logging.debug(f"{name}: Account IDs received: {account_ids}")
            fennel_obj.set_logged_in_object(name, fb, "fb")
            for i, an in enumerate(account_ids):
                account_name = f"Account {i + 1}"
                logging.debug(f"{name}: Retrieving portfolio summary for {account_name} (account id: {an})...")
                b = fb.get_portfolio_summary(an)
                fennel_obj.set_account_number(name, account_name)
                fennel_obj.set_account_totals(
                    name,
                    account_name,
                    b["cash"]["balance"]["canTrade"],
                )
                fennel_obj.set_logged_in_object(name, an, account_name)
                logging.info(f"{name}: Found {account_name}")
            logging.info(f"{name}: Logged in successfully")
        except Exception as e:
            try:
                raw_resp = fb.last_response.text if hasattr(fb, 'last_response') else 'No last_response available'
                logging.error(f"{name}: Raw API response at final error catch: {raw_resp}")
            except Exception:
                logging.error(f"{name}: Could not retrieve raw API response in final error catch.")
            logging.error(f"{name}: Error logging into Fennel: {e}")
            logging.error(traceback.format_exc())
            continue
    logging.info("Finished logging into Fennel!")
    return fennel_obj


def fennel_holdings(fbo: Brokerage, loop=None):
    for key in fbo.get_account_numbers():
        for account in fbo.get_account_numbers(key):
            obj: Fennel = fbo.get_logged_in_objects(key, "fb")
            account_id = fbo.get_logged_in_objects(key, account)
            try:
                positions = obj.get_stock_holdings(account_id)
                if positions != []:
                    for holding in positions:
                        qty = holding["investment"]["ownedShares"]
                        if float(qty) == 0:
                            continue
                        sym = holding["security"]["ticker"]
                        cp = holding["security"]["currentStockPrice"]
                        if cp is None:
                            cp = "N/A"
                        fbo.set_holdings(key, account, sym, qty, cp)
            except Exception as e:
                printAndDiscord(f"Error getting Fennel holdings: {e}")
                print(traceback.format_exc())
                continue
    printHoldings(fbo, loop, False)


def fennel_transaction(fbo: Brokerage, orderObj: stockOrder, loop=None):
    print()
    print("==============================")
    print("Fennel")
    print("==============================")
    print()
    for s in orderObj.get_stocks():
        for key in fbo.get_account_numbers():
            printAndDiscord(
                f"{key}: {orderObj.get_action()}ing {orderObj.get_amount()} of {s}",
                loop,
            )
            for account in fbo.get_account_numbers(key):
                obj: Fennel = fbo.get_logged_in_objects(key, "fb")
                account_id = fbo.get_logged_in_objects(key, account)
                try:
                    order = obj.place_order(
                        account_id=account_id,
                        ticker=s,
                        quantity=orderObj.get_amount(),
                        side=orderObj.get_action(),
                        dry_run=orderObj.get_dry(),
                    )
                    if orderObj.get_dry():
                        message = "Dry Run Success"
                        if not order.get("dry_run_success", False):
                            message = "Dry Run Failed"
                    else:
                        message = "Success"
                        if order.get("data", {}).get("createOrder") != "pending":
                            message = order.get("data", {}).get("createOrder")
                    printAndDiscord(
                        f"{key}: {orderObj.get_action()} {orderObj.get_amount()} of {s} in {account}: {message}",
                        loop,
                    )
                except Exception as e:
                    printAndDiscord(f"{key} {account}: Error placing order: {e}", loop)
                    print(traceback.format_exc())
                    continue
