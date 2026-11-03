from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from config_core.models import UserRole
from config_core.permissions import STAFF


class Command(BaseCommand):
    help = (
        'Creates the default staff roles and gives each user the roles that match the Django groups they are in. '
        'Safe to run more than once.'
    )

    def handle(self, *args, **options):
        for name in STAFF:
            UserRole.objects.get_or_create(name=name)

        copied = 0
        for group in Group.objects.filter(name__in=STAFF):
            role = UserRole.objects.get(name=group.name)
            for user in get_user_model().objects.filter(groups=group).exclude(roles=role):
                user.roles.add(role)
                copied += 1
        self.stdout.write(self.style.SUCCESS(f'{len(STAFF)} roles ready; {copied} user(s) given a role from their group.'))
