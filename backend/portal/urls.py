from django.urls import path

from . import views

app_name = 'portal'

urlpatterns = [
    path('tenant/home/', views.TenantHomeView.as_view(), name='tenant-home'),
    path('tenant/invoices/', views.TenantInvoiceListView.as_view(), name='tenant-invoices'),
    path('tenant/invoices/<uuid:pk>/', views.TenantInvoiceDetailView.as_view(), name='tenant-invoice'),
    path('tenant/payments/', views.TenantPaymentListView.as_view(), name='tenant-payments'),
    path('tenant/lease/', views.TenantLeaseView.as_view(), name='tenant-lease'),
    path('landlord/overview/', views.LandlordOverviewView.as_view(), name='landlord-overview'),
    path('landlord/properties/', views.LandlordPropertyListView.as_view(), name='landlord-properties'),
    path('landlord/properties/<int:pk>/', views.LandlordPropertyDetailView.as_view(), name='landlord-property'),
    path('landlord/statements/', views.LandlordStatementsView.as_view(), name='landlord-statements'),
]
