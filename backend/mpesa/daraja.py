"""
Safaricom Daraja client: the three calls this system makes (OAuth token, C2B URL registration, STK push and its
query). Plain urllib, no extra dependency. Credentials come from settings (environment variables); nothing here is
ever logged or returned to the browser.
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings

SANDBOX = 'https://sandbox.safaricom.co.ke'
PRODUCTION = 'https://api.safaricom.co.ke'
NAIROBI = ZoneInfo('Africa/Nairobi')

_token_lock = threading.Lock()
_token: dict = {'value': '', 'expires_at': 0.0}


class DarajaError(Exception):
    """Safaricom refused the request, could not be reached, or this system is not set up for M-Pesa yet."""


def base_url() -> str:
    override = getattr(settings, 'MPESA_BASE_URL', '')
    if override:
        return override.rstrip('/')
    return PRODUCTION if settings.MPESA_ENV == 'production' else SANDBOX


def missing_settings(*, stk: bool = False) -> list[str]:
    """Names of the settings still empty. STK push also needs the passkey and a public URL for the callback."""
    needed = {
        'MPESA_CONSUMER_KEY': settings.MPESA_CONSUMER_KEY,
        'MPESA_CONSUMER_SECRET': settings.MPESA_CONSUMER_SECRET,
        'MPESA_PAYBILL': settings.MPESA_PAYBILL,
        'MPESA_CALLBACK_SECRET': settings.MPESA_CALLBACK_SECRET,
        'MPESA_PUBLIC_URL': settings.MPESA_PUBLIC_URL,
    }
    if stk:
        needed['MPESA_PASSKEY'] = settings.MPESA_PASSKEY
    return [name for name, value in needed.items() if not value]


def callback_url(kind: str) -> str:
    """Full public URL Safaricom calls. The path avoids the word "mpesa", which Daraja rejects in registered URLs."""
    return f"{settings.MPESA_PUBLIC_URL.rstrip('/')}/api/v1/payments/c2b/{settings.MPESA_CALLBACK_SECRET}/{kind}/"


def _call(method: str, path: str, *, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f'{base_url()}{path}', data=data, method=method,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json', **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)
        except ValueError:
            detail = {}
        message = detail.get('errorMessage') or detail.get('ResponseDescription') or detail.get('error_description') or f'Safaricom answered {e.code}.'
        err = DarajaError(str(message))
        err.code = str(detail.get('errorCode') or e.code)
        raise err from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise DarajaError('Could not reach Safaricom. Try again in a moment.') from e


def access_token() -> str:
    with _token_lock:
        if _token['value'] and _token['expires_at'] > time.time() + 30:
            return _token['value']
        raw = f'{settings.MPESA_CONSUMER_KEY}:{settings.MPESA_CONSUMER_SECRET}'.encode()
        data = _call('GET', '/oauth/v1/generate?grant_type=client_credentials',
                     headers={'Authorization': 'Basic ' + base64.b64encode(raw).decode()})
        token = data.get('access_token')
        if not token:
            raise DarajaError('Safaricom did not return an access token. Check the consumer key and secret.')
        _token.update(value=token, expires_at=time.time() + int(data.get('expires_in') or 3000))
        return token


def _authed(path: str, body: dict):
    return _call('POST', path, body=body, headers={'Authorization': f'Bearer {access_token()}'})


def register_urls() -> dict:
    """Tells Safaricom where to send C2B validation and confirmation messages for our paybill."""
    problems = missing_settings()
    if problems:
        raise DarajaError('Not set up yet: ' + ', '.join(problems) + '.')
    return _authed('/mpesa/c2b/v2/registerurl', {
        'ShortCode': settings.MPESA_PAYBILL,
        # Accept payments even if our validation URL cannot be reached; we sort them out afterwards.
        'ResponseType': 'Completed',
        'ConfirmationURL': callback_url('confirmation'),
        'ValidationURL': callback_url('validation'),
    })


def _stk_password(timestamp: str) -> str:
    return base64.b64encode(f'{settings.MPESA_PAYBILL}{settings.MPESA_PASSKEY}{timestamp}'.encode()).decode()


def stk_push(*, phone: str, amount: int, account_reference: str, description: str = 'Rent') -> dict:
    """Asks Safaricom to show the PIN prompt on the tenant's phone. Returns MerchantRequestID and CheckoutRequestID."""
    problems = missing_settings(stk=True)
    if problems:
        raise DarajaError('Not set up yet: ' + ', '.join(problems) + '.')
    stamp = datetime.now(NAIROBI).strftime('%Y%m%d%H%M%S')
    data = _authed('/mpesa/stkpush/v1/processrequest', {
        'BusinessShortCode': settings.MPESA_PAYBILL,
        'Password': _stk_password(stamp),
        'Timestamp': stamp,
        'TransactionType': 'CustomerPayBillOnline',
        'Amount': int(amount),
        'PartyA': phone,
        'PartyB': settings.MPESA_PAYBILL,
        'PhoneNumber': phone,
        'CallBackURL': callback_url('stk'),
        'AccountReference': account_reference[:12],
        'TransactionDesc': description[:13],
    })
    if str(data.get('ResponseCode')) != '0' or not data.get('CheckoutRequestID'):
        raise DarajaError(data.get('ResponseDescription') or data.get('CustomerMessage') or 'Safaricom did not accept the request.')
    return data


def stk_query(checkout_request_id: str) -> dict:
    stamp = datetime.now(NAIROBI).strftime('%Y%m%d%H%M%S')
    return _authed('/mpesa/stkpushquery/v1/query', {
        'BusinessShortCode': settings.MPESA_PAYBILL,
        'Password': _stk_password(stamp),
        'Timestamp': stamp,
        'CheckoutRequestID': checkout_request_id,
    })
