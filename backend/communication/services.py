"""Text messaging: who gets a message, what it says, and handing it to the SMS provider."""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

from config_core.models import AgencyProfile, SMSTemplate
from properties.models import Landlord
from tenants.models import Tenant
from tenants.utils import normalize_phone_number
from .models import SmsMessage

logger = logging.getLogger(__name__)

TENANT_FIELDS = {'name', 'first_name', 'unit', 'property', 'balance', 'rent', 'tenant_id', 'paybill', 'agency'}
LANDLORD_FIELDS = {'name', 'first_name', 'agency'}
PLACEHOLDER = re.compile(r'\{(\w+)\}')


class SmsError(ValueError):
    pass


def provider_name() -> str:
    return 'africastalking' if settings.SMS_PROVIDER == 'africastalking' and settings.AT_API_KEY else 'console'


def unknown_placeholders(message: str, allowed: set[str]) -> list[str]:
    return sorted({m for m in PLACEHOLDER.findall(message) if m not in allowed})


def render(message: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), message)


def money(value) -> str:
    return f"KES {Decimal(value):,.0f}"


def tenant_values(tenant: Tenant, agency: str) -> dict[str, str]:
    balance = sum((i.balance for i in tenant.invoices.all()), Decimal('0'))
    unit = tenant.unit
    return {
        'name': tenant.name, 'first_name': tenant.name.split()[0] if tenant.name else '',
        'unit': unit.unit_name if unit else '', 'property': unit.property.name if unit else '',
        'balance': money(max(balance, 0)), 'rent': money(unit.rent_amount) if unit else '',
        'tenant_id': tenant.tenant_id, 'paybill': settings.MPESA_PAYBILL, 'agency': agency,
    }


def landlord_values(landlord: Landlord, agency: str) -> dict[str, str]:
    return {'name': landlord.name, 'first_name': landlord.name.split()[0] if landlord.name else '', 'agency': agency}


@dataclass
class Outgoing:
    name: str
    phone: str
    body: str
    tenant: Tenant | None = None
    landlord: Landlord | None = None


def build_messages(message: str, tenant_ids: list[int], landlord_ids: list[int]) -> list[Outgoing]:
    """One personalised message per recipient. People without a phone number are skipped by the caller."""
    agency = (AgencyProfile.objects.first() or AgencyProfile(company_name='GeoPima')).company_name
    out: list[Outgoing] = []
    tenants = (
        Tenant.objects.filter(pk__in=tenant_ids).select_related('unit__property').prefetch_related('invoices__items', 'invoices__settlements')
    )
    for t in tenants:
        out.append(Outgoing(t.name, t.phone_number, render(message, tenant_values(t, agency)), tenant=t))
    for l in Landlord.objects.filter(pk__in=landlord_ids):
        out.append(Outgoing(l.name, normalize_phone_number(l.phone_number), render(message, landlord_values(l, agency)), landlord=l))
    return out


def _send_africastalking(phone: str, body: str) -> None:
    host = 'api.sandbox.africastalking.com' if settings.AT_USERNAME == 'sandbox' else 'api.africastalking.com'
    form = {'username': settings.AT_USERNAME, 'to': f'+{phone}', 'message': body}
    if settings.AT_SENDER_ID:
        form['from'] = settings.AT_SENDER_ID
    req = urllib.request.Request(
        f'https://{host}/version1/messaging', data=urllib.parse.urlencode(form).encode(),
        headers={'apiKey': settings.AT_API_KEY, 'Accept': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        raise SmsError(f'The SMS provider answered {e.code}.') from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SmsError('Could not reach the SMS provider.') from e
    recipients = data.get('SMSMessageData', {}).get('Recipients') or []
    if not recipients or recipients[0].get('statusCode') not in (100, 101, 102):
        status = recipients[0].get('status') if recipients else data.get('SMSMessageData', {}).get('Message')
        raise SmsError(str(status or 'The SMS provider rejected the message.'))


def deliver(phone: str, body: str) -> str:
    """Hands one message to the provider. Returns 'sent' or 'test'; raises SmsError on failure."""
    if provider_name() == 'africastalking':
        _send_africastalking(phone, body)
        return 'sent'
    logger.info('SMS (no provider configured) to %s: %s', phone, body)
    return 'test'


def send_batch(messages: list[Outgoing], user, payment_ref: str = '') -> dict:
    batch = uuid.uuid4()
    counts = {'sent': 0, 'failed': 0, 'test': 0, 'skipped': 0}
    for m in messages:
        if len(m.phone) < 11:
            counts['skipped'] += 1
            continue
        error = ''
        try:
            status = deliver(m.phone, m.body)
        except SmsError as e:
            status, error = 'failed', str(e)
        except Exception:
            logger.exception('SMS to %s failed', m.phone)
            status, error = 'failed', 'Unexpected error while sending.'
        counts[status] += 1
        SmsMessage.objects.create(
            batch=batch, tenant=m.tenant, landlord=m.landlord, recipient_name=m.name, phone=m.phone,
            body=m.body, status=status, error=error, sent_by=user, payment_ref=payment_ref,
        )
    return {'batch': str(batch), **counts}


# ---------------------------------------------------------------- automatic "payment received" text

RECEIPT_FIELDS = TENANT_FIELDS | {'amount', 'receipt', 'date'}
DEFAULT_RECEIPT_TEXT = (
    'Dear {first_name}, we received {amount} on {date} (ref {receipt}). Your balance is {balance}. Thank you, {agency}.'
)


def _payment_date(tx) -> str:
    """The day the money was paid: Safaricom and manual entries both store YYYYMMDDHHMMSS; fall back to when we got it."""
    from datetime import datetime
    try:
        day = datetime.strptime(tx.trans_time[:8], '%Y%m%d')
    except ValueError:
        day = tx.created_at
    return f'{day.day} {day:%b %Y}'


def send_payment_receipt(transaction_id: int, tenant_id: int) -> None:
    """Texts the tenant that their payment was received. Once per payment; never raises, so it can't affect the payment."""
    from mpesa.models import MpesaTransaction
    try:
        tx = MpesaTransaction.objects.get(pk=transaction_id)
        template = SMSTemplate.objects.filter(category='PAYMENT_NOTIF').first()
        if template and not template.enabled:
            return
        if SmsMessage.objects.filter(payment_ref=tx.trans_id).exists():
            return
        tenant = (
            Tenant.objects.select_related('unit__property').prefetch_related('invoices__items', 'invoices__settlements')
            .get(pk=tenant_id)
        )
        agency = (AgencyProfile.objects.first() or AgencyProfile(company_name='GeoPima')).company_name
        text = template.template_text.strip() if template else ''
        if not text or unknown_placeholders(text, RECEIPT_FIELDS):
            text = DEFAULT_RECEIPT_TEXT  # an unwritten or broken template must not stop receipts
        values = {**tenant_values(tenant, agency), 'amount': money(tx.trans_amount), 'receipt': tx.trans_id, 'date': _payment_date(tx)}
        message = Outgoing(tenant.name, tenant.phone_number, render(text, values), tenant=tenant)
        send_batch([message], None, payment_ref=tx.trans_id)
    except Exception:
        logger.exception('Could not send the payment receipt for transaction %s', transaction_id)


def notify_payment_received(transaction_id: int, tenant_id: int) -> None:
    """
    Called once a payment is safely saved. Sending takes a network round trip, and Safaricom wants a quick answer to
    its callback, so by default this runs in the background.
    """
    def run():
        from django.db import connection
        try:
            send_payment_receipt(transaction_id, tenant_id)
        finally:
            connection.close()

    if settings.SMS_BACKGROUND:
        threading.Thread(target=run, daemon=True).start()
    else:
        send_payment_receipt(transaction_id, tenant_id)
