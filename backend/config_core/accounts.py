"""Portal logins for tenants and landlords: create the account, link it to the record, hand over a password once."""
import re
import secrets

from django.contrib.auth import get_user_model
from django.db import transaction

# No look-alike characters (0/O, 1/l/I) so a password read out over a call or SMS is typed correctly.
_ALPHABET = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def make_password(length: int = 10) -> str:
    return ''.join(secrets.choice(_ALPHABET) for _ in range(length))


def _username(base: str) -> str:
    User = get_user_model()
    base = re.sub(r'[^\w.@+-]', '', base)[:140] or 'user'
    name, n = base, 1
    while User.objects.filter(username__iexact=name).exists():
        n += 1
        name = f'{base}{n}'
    return name


@transaction.atomic
def issue_login(profile, username_hint: str, email: str | None = None) -> dict:
    """
    Give a tenant or landlord record a portal login. If it already has one, set a new password for it instead.
    Returns the username and the new password; the password is never stored in readable form, so show it once.
    """
    User = get_user_model()
    password = make_password()
    user = profile.user
    created = user is None
    if created:
        first, _, last = (profile.name or '').partition(' ')
        user = User(
            username=_username(username_hint), first_name=first, last_name=last,
            email=email or '', phone=getattr(profile, 'phone_number', None) or None,
        )
        user.set_password(password)
        user.save()
        profile.user = user
        profile.save(update_fields=['user'])
    else:
        user.set_password(password)
        user.is_active = True
        user.save(update_fields=['password', 'is_active'])
    return {'username': user.username, 'password': password, 'created': created}
