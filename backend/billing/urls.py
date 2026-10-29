from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import DepositViewSet, InvoiceViewSet, VoteHeadViewSet

app_name = 'billing'

router = DefaultRouter()
router.register(r'vote-heads', VoteHeadViewSet, basename='vote-head')
router.register(r'deposits', DepositViewSet, basename='deposit')
router.register(r'invoices', InvoiceViewSet, basename='invoice')

urlpatterns = [
    path('', include(router.urls)),
]
