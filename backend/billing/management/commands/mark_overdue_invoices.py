from django.core.management.base import BaseCommand

from billing.services import mark_overdue


class Command(BaseCommand):
    help = "Marks unpaid invoices that are past their due date as overdue. Run daily."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(f"{mark_overdue()} invoice(s) marked overdue."))
