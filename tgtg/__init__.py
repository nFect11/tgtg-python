import datetime
import random
import sys
import time
from http import HTTPStatus
from urllib.parse import urljoin
import json
import asyncio

import requests
from tomlkit import item

from tgtg.google_play_scraper import get_last_apk_version

from .exceptions import TgtgAPIError, TgtgLoginError, TgtgPollingError

BASE_URL = "https://apptoogoodtogo.com/api/"
API_ITEM_ENDPOINT = "item/v7/"
API_ORDER_CREATE = "order/v6/create/"
API_ORDER = "order/v6/"
API_PAYMENT = "payment/v3/"
AUTH_BY_EMAIL_ENDPOINT = "auth/v3/authByEmail"
AUTH_POLLING_ENDPOINT = "auth/v3/authByRequestPollingId"
SIGNUP_BY_EMAIL_ENDPOINT = "auth/v3/signUpByEmail"
REFRESH_ENDPOINT = "auth/v3/token/refresh"
ACTIVE_ORDER_ENDPOINT = "order/v6/active"
INACTIVE_ORDER_ENDPOINT = "order/v6/inactive"
DEFAULT_APK_VERSION = "22.5.5"
USER_AGENTS = [
    "TGTG/{} Dalvik/2.1.0 (Linux; U; Android 9; Nexus 5 Build/M4B30Z)",
    "TGTG/{} Dalvik/2.1.0 (Linux; U; Android 10; SM-G935F Build/NRD90M)",
    "TGTG/{} Dalvik/2.1.0 (Linux; Android 12; SM-G920V Build/MMB29K)",
]
DEFAULT_ACCESS_TOKEN_LIFETIME = 3600 * 4  # 4 hours
MAX_POLLING_TRIES = 24  # 24 * POLLING_WAIT_TIME = 2 minutes
POLLING_WAIT_TIME = 5  # Seconds


class TgtgClient:
    def __init__(
        self,
        url=BASE_URL,
        email=None,
        access_token=None,
        refresh_token=None,
        user_id=None,
        user_agent=None,
        language="en-UK",
        proxies=None,
        timeout=None,
        access_token_lifetime=DEFAULT_ACCESS_TOKEN_LIFETIME,
        device_type="ANDROID",
    ):

        self.base_url = url

        self.email = email

        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_id = user_id

        self.last_time_token_refreshed = None
        self.access_token_lifetime = access_token_lifetime

        self.device_type = device_type

        self.user_agent = user_agent if user_agent else self._get_user_agent()
        self.language = language
        self.proxies = proxies
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers = self._headers

    def _get_user_agent(self):
        try:
            self.version = get_last_apk_version()
        except Exception:
            self.version = DEFAULT_APK_VERSION
            sys.stdout.write("Failed to get last version\n")

        sys.stdout.write(f"Using version {self.version}\n")

        return random.choice(USER_AGENTS).format(self.version)

    def _get_url(self, path):
        return urljoin(self.base_url, path)

    def get_credentials(self):
        self.login()
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "user_id": self.user_id,
        }

    @property
    def _headers(self):
        headers = {
            "user-agent": self.user_agent,
            "accept-language": self.language,
            "Accept-Encoding": "gzip",
        }
        if self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
        return headers

    @property
    def _already_logged(self):
        return bool(self.access_token and self.refresh_token and self.user_id)

    def _refresh_token(self):
        if (
            self.last_time_token_refreshed
            and (datetime.datetime.now() - self.last_time_token_refreshed).seconds
            <= self.access_token_lifetime
        ):
            return

        response = self.session.post(
            self._get_url(REFRESH_ENDPOINT),
            json={"refresh_token": self.refresh_token},
            headers=self._headers,
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code == HTTPStatus.OK:
            self.access_token = response.json()["access_token"]
            self.refresh_token = response.json()["refresh_token"]
            self.last_time_token_refreshed = datetime.datetime.now()
        else:
            raise TgtgAPIError(response.status_code, response.content)

    def login(self):
        if not (
            self.email or self.access_token and self.refresh_token and self.user_id
        ):
            raise TypeError(
                "You must provide at least email or access_token, refresh_token and user_id"
            )
        if self._already_logged:
            self._refresh_token()
        else:
            response = self.session.post(
                self._get_url(AUTH_BY_EMAIL_ENDPOINT),
                headers=self._headers,
                json={
                    "device_type": self.device_type,
                    "email": self.email,
                },
                proxies=self.proxies,
                timeout=self.timeout,
            )
            if response.status_code == HTTPStatus.OK:
                first_login_response = response.json()
                if first_login_response["state"] == "TERMS":
                    raise TgtgPollingError(
                        f"This email {self.email} is not linked to a tgtg account. "
                        "Please signup with this email first."
                    )
                elif first_login_response["state"] == "WAIT":
                    self.start_polling(first_login_response["polling_id"])
                else:
                    raise TgtgLoginError(response.status_code, response.content)
            else:
                if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                    raise TgtgAPIError(
                        response.status_code, "Too many requests. Try again later."
                    )
                else:
                    raise TgtgLoginError(response.status_code, response.content)

    def start_polling(self, polling_id):
        for _ in range(MAX_POLLING_TRIES):
            response = self.session.post(
                self._get_url(AUTH_POLLING_ENDPOINT),
                headers=self._headers,
                json={
                    "device_type": self.device_type,
                    "email": self.email,
                    "request_polling_id": polling_id,
                },
                proxies=self.proxies,
                timeout=self.timeout,
            )
            if response.status_code == HTTPStatus.ACCEPTED:
                sys.stdout.write(
                    "Check your mailbox on PC to continue... "
                    "(Mailbox on mobile won't work, if you have installed tgtg app.)\n"
                )
                time.sleep(POLLING_WAIT_TIME)
                continue
            elif response.status_code == HTTPStatus.OK:
                sys.stdout.write("Logged in!\n")
                login_response = response.json()
                self.access_token = login_response["access_token"]
                self.refresh_token = login_response["refresh_token"]
                self.last_time_token_refreshed = datetime.datetime.now()
                self.user_id = login_response["startup_data"]["user"]["user_id"]
                return
            else:
                if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                    raise TgtgAPIError(
                        response.status_code, "Too many requests. Try again later."
                    )
                else:
                    raise TgtgLoginError(response.status_code, response.content)

        raise TgtgPollingError(
            f"Max retries ({MAX_POLLING_TRIES * POLLING_WAIT_TIME} seconds) reached. Try again."
        )

    def get_items(
        self,
        *,
        latitude=0.0,
        longitude=0.0,
        radius=21,
        page_size=20,
        page=1,
        discover=False,
        favorites_only=True,
        item_categories=None,
        diet_categories=None,
        pickup_earliest=None,
        pickup_latest=None,
        search_phrase=None,
        with_stock_only=False,
        hidden_only=False,
        we_care_only=False,
    ):
        self.login()

        # fields are sorted like in the app
        data = {
            "user_id": self.user_id,
            "origin": {"latitude": latitude, "longitude": longitude},
            "radius": radius,
            "page_size": page_size,
            "page": page,
            "discover": discover,
            "favorites_only": favorites_only,
            "item_categories": item_categories if item_categories else [],
            "diet_categories": diet_categories if diet_categories else [],
            "pickup_earliest": pickup_earliest,
            "pickup_latest": pickup_latest,
            "search_phrase": search_phrase if search_phrase else None,
            "with_stock_only": with_stock_only,
            "hidden_only": hidden_only,
            "we_care_only": we_care_only,
        }
        response = self.session.post(
            self._get_url(API_ITEM_ENDPOINT),
            headers=self._headers,
            json=data,
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code == HTTPStatus.OK:
            return response.json()["items"]
        else:
            raise TgtgAPIError(response.status_code, response.content)

    def get_item(self, item_id):
        self.login()
        response = self.session.post(
            urljoin(self._get_url(API_ITEM_ENDPOINT), str(item_id)),
            headers=self._headers,
            json={"user_id": self.user_id, "origin": None},
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code == HTTPStatus.OK:
            return response.json()
        else:
            raise TgtgAPIError(response.status_code, response.content)

    def set_favorite(self, item_id, is_favorite):
        self.login()
        response = self.session.post(
            urljoin(self._get_url(API_ITEM_ENDPOINT), f"{item_id}/setFavorite"),
            headers=self._headers,
            json={"is_favorite": is_favorite},
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code != HTTPStatus.OK:
            raise TgtgAPIError(response.status_code, response.content)

    def signup_by_email(
        self,
        *,
        email,
        name="",
        country_id="GB",
        newsletter_opt_in=False,
        push_notification_opt_in=True,
    ):
        response = self.session.post(
            self._get_url(SIGNUP_BY_EMAIL_ENDPOINT),
            headers=self._headers,
            json={
                "country_id": country_id,
                "device_type": self.device_type,
                "email": email,
                "name": name,
                "newsletter_opt_in": newsletter_opt_in,
                "push_notification_opt_in": push_notification_opt_in,
            },
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code == HTTPStatus.OK:
            self.access_token = response.json()["login_response"]["access_token"]
            self.refresh_token = response.json()["login_response"]["refresh_token"]
            self.last_time_token_refreshed = datetime.datetime.now()
            self.user_id = response.json()["login_response"]["startup_data"]["user"][
                "user_id"
            ]
            return self
        else:
            raise TgtgAPIError(response.status_code, response.content)

    def get_active(self):
        self.login()
        response = self.session.post(
            self._get_url(ACTIVE_ORDER_ENDPOINT),
            headers=self._headers,
            json={"user_id": self.user_id},
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code == HTTPStatus.OK:
            return response.json()
        else:
            raise TgtgAPIError(response.status_code, response.content)

    def get_inactive(self, page=0, page_size=20):
        self.login()
        response = self.session.post(
            self._get_url(INACTIVE_ORDER_ENDPOINT),
            headers=self._headers,
            json={"paging": {"page": page, "size": page_size}, "user_id": self.user_id},
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code == HTTPStatus.OK:
            return response.json()
        else:
            raise TgtgAPIError(response.status_code, response.content)
    
    def order_item(self, item_id, requested_bags = 1):

        self.login()
        print("Checking stock...")
        checkStock = self.get_item(item_id)
        checkStock = checkStock["items_available"]
        if(checkStock == 0):
            return
        elif(checkStock > 0):
            print(f"Stock at: {checkStock} items")
            print("Sending buy order...")
            if(checkStock < requested_bags):
                requested_bags = checkStock
            response = self.session.post(
                urljoin(self._get_url(API_ORDER_CREATE), str(item_id)),
                headers=self._headers,
                json={"item_count": requested_bags},
                proxies=self.proxies,
                timeout=self.timeout,
        )
        if response.status_code == HTTPStatus.OK:
            order_resp = response.json()
            print(f"Buy Order: {order_resp['state']}\nOrder-ID: {order_resp['order']['id']}\nState: {order_resp['order']['state']}")
            if(order_resp["state"] == "SUCCESS" and order_resp["order"]["state"] == "RESERVED"):
                order_id = order_resp["order"]["id"]
                print("Initiating bank transfer...")
                return self.order_bank_transfer(order_id)
            return
        else:
            raise TgtgAPIError(response.status_code, response.content)

    def order_bank_transfer(self, order_id):
        order_status = self.session.post(
            urljoin(self._get_url(API_ORDER), f"{order_id}/status"),
            headers=self._headers,
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if(order_status.status_code == HTTPStatus.OK):
            order_status = order_status.json()
            print(f"Order Status: {order_status}")
            if order_status["state"] == "RESERVED":       
                bank_json = {"authorization":
                                {"authorization_payload":
                                    {"save_payment_method":False,
                                    "payment_type":"SOFORT",
                                    "type":"adyenAuthorizationPayload",
                                    "payload":"{\"name\":\"Online bank transfer.\",\"type\":\"directEbanking\"}"},
                                "payment_provider":"ADYEN",
                                "return_url":"adyencheckout://com.app.tgtg.itemview"}
                            }
                payment_id = self.session.post(
                    urljoin(self._get_url(API_ORDER), f"{order_id}/pay"),
                    headers=self._headers,
                    json=bank_json,
                    proxies=self.proxies,
                    timeout=self.timeout,
                )
                print(f"Bank Response: {payment_id}")
                if(payment_id.status_code == HTTPStatus.OK):
                    payment_id = payment_id.json()
                    payment_id = payment_id["payment_id"]
                    return self.pay_bank(payment_id)

                else:
                    raise TgtgAPIError(payment_id.status_code, payment_id.content)
        else:
            raise TgtgAPIError(order_status.status_code, order_status.content)
    def pay_bank(self, payment_id):
        time.sleep(6)
        print(f"Payment ID: {payment_id}")
        #while not payment_response:
        payment_response = self.session.post(
                urljoin(self._get_url(API_PAYMENT), str(payment_id)),
                headers=self._headers,
                proxies=self.proxies,
                timeout=self.timeout,
            )
        payment_response = payment_response.json()
        print(f"Payment reponse: {payment_response}")
        if(payment_response["state"] == "ADDITIONAL_AUTHORIZATION_REQUIRED"):
            payload = json.loads(payment_response["payload"])
            payload_url = payload["url"]
            print(payload_url)
            return payload_url
        return "No Payment URL available"
            
        