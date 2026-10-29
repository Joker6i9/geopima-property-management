from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import MpesaConfirmationView, MpesaTransactionViewSet, MpesaValidationView

app_name = 'mpesa'

router = DefaultRouter()
router.register(r'transactions', MpesaTransactionViewSet, basename='transaction')

urlpatterns = [
    # Safaricom callbacks. <secret> must equal MPESA_CALLBACK_SECRET; register these full URLs with Daraja.
    path('c2b/<str:secret>/validation/', MpesaValidationView.as_view(), name='c2b_validation'),
    path('c2b/<str:secret>/confirmation/', MpesaConfirmationView.as_view(), name='c2b_confirmation'),
    path('', include(router.urls)),
]
