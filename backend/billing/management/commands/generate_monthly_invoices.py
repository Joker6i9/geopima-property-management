from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError

from billing.services import generate_invoices, mark_overdue


class Command(BaseCommand):
    help = 'Bills every active lease for a month (default: this month). Safe to run more than once.'

    def add_arguments(self, parser):
        parser.add_argument('--period', help='Billing month as YYYY-MM. Defaults to the current month.')

    def handle(self, *args, **options):
        if options['period']:
            try:
                period = datetime.strptime(options['period'], '%Y-%m').date()
            except ValueError:
                raise CommandError('--period must look like 2026-10')
        else:
            period = date.today()

        result = generate_invoices(period)
        overdue = mark_overdue()
        self.stdout.write(self.style.SUCCESS(
            f"{period:%B %Y}: created {len(result.created)} invoice(s), skipped {result.skipped} that already existed; "
            f"{overdue} invoice(s) marked overdue."
        ))
